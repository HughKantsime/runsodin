"""Security review close-out: every route that writes credential/recovery material,
or reads it in bulk, must be interactive-session only (no API token, no global key).

Two layers:

1. A static census over every route module. It parses each ``@router.<verb>`` route
   with ``ast``, reads the SQL/ORM writes and reads in the route function AND in the
   module-level helpers it calls, and classifies the route as credential-guarded when
   it touches a guarded table/column or reads bulk credential material. Every guarded
   route must declare ``require_interactive_session`` (directly or via a route-level
   dependency), or be in REVIEWED_PUBLIC (carriers that authenticate by their own
   one-time secret, or first-run). A new route that touches these tables without the
   guard fails this test — fail closed.

2. A live refusal probe (test_cert_F_B_api_tokens) exercises the enforcement itself.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "backend"
MODULES = sorted(p for p in BACKEND.glob("modules/**/*.py") if "@router" in p.read_text())

# Guarded write targets: authentication / account-recovery / credential material.
WRITE_TABLES = {
    "api_tokens", "biometric_tokens", "push_subscriptions", "push_devices",
    "active_sessions", "oidc_config", "password_reset_tokens",
}
# users is guarded only for credential/recovery columns, not role/is_active/group_id/theme.
USERS_CREDENTIAL_COLS = {"email", "username", "password_hash", "mfa_enabled", "mfa_secret",
                         "oidc_issuer", "oidc_subject", "oidc_provider"}
# system_config keys that are credential/transport/setup material.
CONFIG_KEYS = {"smtp_config", "base_url", "setup_complete", "setup_admin_claimed", "oidc"}
# Helpers that encapsulate a guarded write; a route that calls one is guarded.
GUARDED_HELPERS = {
    "hash_password", "create_biometric_token", "invalidate_recovery_credentials",
    "cancel_pending_enrollments", "_resolve_oidc_user", "record_session", "stage_session",
}
# Bulk credential reads (hashes/secrets/tokens leave the process).
WRITE_MODELS = {"BiometricToken", "ApiToken", "OIDCConfig", "PasswordResetToken"}

STMT = re.compile(
    r"(INSERT\s+(?:OR\s+IGNORE\s+)?INTO|UPDATE|DELETE\s+FROM)\s+(\w+)([^;\"\']*)", re.I)

# Routes that legitimately touch this material without an interactive session:
# carriers that authenticate by their own one-time secret, or first-run setup.
REVIEWED_PUBLIC = {
    ("POST", "/auth/login"),            # writes last_login + its own session
    ("POST", "/auth/logout"),           # clears the caller's own session
    ("POST", "/auth/mfa/verify"),       # consumes a signed MFA challenge
    ("POST", "/auth/reset-password"),   # one-time reset token is the credential
    ("POST", "/auth/forgot-password"),  # issues a reset token to the stored email
    ("POST", "/auth/biometric-refresh"),# the refresh token is the credential
    ("GET", "/auth/oidc/callback"), ("POST", "/auth/oidc/exchange"),  # IdP code is the credential
    ("POST", "/setup/admin"),           # first-run claim; gated by setup phase
    ("POST", "/auth/mfa/enrollment/setup"),   # login-time MFA enrollment carrier
    ("POST", "/auth/mfa/enrollment/confirm"),
    ("POST", "/auth/mfa/enrollment/cancel"),
    # Time-based garbage collection of expired/stale rows, not a credential change.
    ("POST", "/admin/retention/cleanup"),
}


def _call_names(node: ast.AST) -> set[str]:
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def _string_literals(node: ast.AST) -> str:
    return " ".join(s.value for s in ast.walk(node)
                    if isinstance(s, ast.Constant) and isinstance(s.value, str))


def _route_decorator(fn: ast.AST):
    for dec in getattr(fn, "decorator_list", []):
        if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in {"get", "post", "put", "patch", "delete", "api_route"}
                and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"):
            return dec
    return None


def _decorator_text(dec, src) -> str:
    return ast.get_source_segment(src, dec) or ""


def _method_path(dec, src):
    path = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else "?"
    attr = dec.func.attr
    if attr == "api_route":
        methods = _string_literals(dec).upper()
        method = "GET" if "GET" in methods else "POST"
    else:
        method = attr.upper()
    return method, path


def _module_helpers(tree):
    return {fn.name: fn for fn in tree.body if isinstance(fn, ast.FunctionDef)
            or isinstance(fn, ast.AsyncFunctionDef)}


def _guarded_signals(fn, src, helpers, _depth=0):
    """Guarded tables/keys/reads this function (and its local helpers) touch."""
    signals = set()
    seg = ast.get_source_segment(src, fn) or ""
    for kw, table, rest in STMT.findall(seg):
        table = table.lower()
        kw = kw.upper()
        if table in WRITE_TABLES:
            # A DELETE by a time cutoff is garbage collection, not a credential change.
            if kw.startswith("DELETE") and re.search(r"(expires_at|last_seen_at|created_at)\s*<", rest):
                continue
            signals.add("table:" + table)
        if table == "users" and not kw.startswith("DELETE"):
            if any(re.search(rf"\b{c}\b", rest) for c in USERS_CREDENTIAL_COLS):
                signals.add("users:credential")
        if table == "system_config" and any(k in rest for k in CONFIG_KEYS):
            signals.add("config:credential")
    if "SystemConfig(" in seg and any(k in seg for k in CONFIG_KEYS):
        signals.add("config:credential")
    calls = _call_names(fn)
    signals |= {"helper:" + h for h in (calls & GUARDED_HELPERS)}
    signals |= {"model:" + m for m in (calls & WRITE_MODELS)}
    # Bulk credential reads: a full system_config dump, a backup file body, GDPR export.
    if re.search(r"SELECT\s+key\s*,\s*value\s+FROM\s+system_config", seg, re.I):
        signals.add("read:config_dump")
    if "FileResponse" in seg and "backup" in seg.lower():
        signals.add("read:backup_content")
    if fn.name in {"export_user_data"}:
        signals.add("read:user_export")
    if _depth < 1:
        for name in calls & helpers.keys():
            signals |= _guarded_signals(helpers[name], src, helpers, _depth + 1)
    return signals


def _collect():
    guarded, enforced = {}, {}
    interactive = _interactive_routes()
    seen_routes = set()
    for module in MODULES:
        src = module.read_text()
        tree = ast.parse(src)
        helpers = _module_helpers(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            dec = _route_decorator(fn)
            if dec is None:
                continue
            method, path = _method_path(dec, src)
            signals = _guarded_signals(fn, src, helpers)
            if signals:
                guarded[(method, path)] = sorted(signals)
            seen_routes.add((method, path))
    # Enforcement is read from the real app's dependency tree, not from source text
    # (a comment naming the guard must not count). Routers mount under different
    # prefixes, so a router-relative path matches the mounted path's suffix.
    for method, path in seen_routes:
        if len(path) > 1 and any(m == method and full.endswith(path) for m, full in interactive):
            enforced[(method, path)] = True
    return guarded, enforced


def _handler_calls_is_interactive(endpoint) -> bool:
    """True only for a real call (not a comment or string) in the handler body."""
    import inspect
    import textwrap
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    except (OSError, TypeError, SyntaxError):
        return False
    return "is_interactive_session" in _call_names(tree)


def _interactive_routes():
    """(method, router-relative path) for every app route whose dependant tree includes
    require_interactive_session's checker, or whose handler calls is_interactive_session."""
    from fastapi.routing import APIRoute
    from core.app import create_app
    from core.rbac import _SCOPE_MARK

    def interactive(dependant, seen):
        for dep in dependant.dependencies:
            if getattr(dep.call, _SCOPE_MARK, None) == "interactive":
                return True
            if id(dep) not in seen and (seen.add(id(dep)) or interactive(dep, seen)):
                return True
        return False

    found = set()
    for route in create_app().routes:
        if not isinstance(route, APIRoute):
            continue
        if interactive(route.dependant, set()) or _handler_calls_is_interactive(route.endpoint):
            for method in route.methods - {"HEAD"}:
                found.add((method, route.path))
    return found


def test_every_credential_write_or_bulk_read_route_is_interactive_only():
    guarded, enforced = _collect()
    unguarded = {route: sig for route, sig in guarded.items()
                 if route not in enforced and route not in REVIEWED_PUBLIC}
    assert unguarded == {}, (
        "Routes that write credential material or read it in bulk but are not "
        "interactive-session only (add require_interactive_session, or add to "
        "REVIEWED_PUBLIC with a reason): " + repr(unguarded))


# Bulk reads whose content can carry live login material (log lines include
# request URLs; the bundle includes logs and config). Pinned explicitly because
# the static signals above cannot see "reads a log file".
REQUIRED_INTERACTIVE_READS = {
    ("GET", "/admin/logs"), ("GET", "/admin/logs/stream"), ("GET", "/admin/support-bundle"),
}


def test_log_and_bundle_reads_are_interactive_only():
    _, enforced = _collect()
    assert REQUIRED_INTERACTIVE_READS <= set(enforced), REQUIRED_INTERACTIVE_READS - set(enforced)


def test_the_census_actually_found_the_known_guarded_routes():
    # Guard against a silently empty census (e.g. a parse regression).
    guarded, enforced = _collect()
    for route in [("POST", "/auth/biometric-token"), ("PUT", "/smtp-config"),
                  ("POST", "/tokens"), ("PATCH", "/users/{user_id}"),
                  ("PUT", "/admin/oidc")]:
        # Each known credential route must be *detected* by the static signals (an
        # empty or broken census fails here) and enforced on the real app.
        assert route in guarded, route
        assert route in enforced or route in REVIEWED_PUBLIC, route
    assert ("POST", "/backups/restore") in enforced  # no static signal; pinned directly
    # Every restore-staging route (cert-fix-migrations) is interactive-session only.
    from modules.system.routes_backup import BACKUP_ROUTES_REQUIRING_INTERACTIVE
    assert ("POST", "/backups/{filename}/stage") in BACKUP_ROUTES_REQUIRING_INTERACTIVE
    assert set(BACKUP_ROUTES_REQUIRING_INTERACTIVE) <= set(enforced), set(BACKUP_ROUTES_REQUIRING_INTERACTIVE) - set(enforced)
    assert len(guarded) >= 25, len(guarded)
    assert len(enforced) >= 25, len(enforced)
