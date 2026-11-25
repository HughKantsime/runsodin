"""Show or disable the IP allowlist from the ODIN host."""
import argparse
from contextlib import redirect_stdout
import ipaddress
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.util import asbool

from core.audit import log_audit
from core.ip_allowlist_cache import cache_ttl_seconds


def _session():
    from core.config import settings

    # core.db opens SQLite on import. Avoid creating an empty database if the
    # configured file has gone missing.
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite":
        path = url.database or ""
        if path.startswith("file:") and asbool(url.query.get("uri", False)):
            path = unquote(urlsplit(path).path)
        if not Path(path).is_file():
            raise FileNotFoundError
    # With DEBUG enabled, the import itself can echo SQL on the first connection.
    with redirect_stdout(sys.stderr):
        from core.db import SessionLocal, engine
    engine.echo = False  # Keep SQL debug output out of the host command.
    return SessionLocal()


def _reject_non_json_number(_):
    raise ValueError("Invalid JSON number")


def _policy(raw):
    """Read the saved format, including older lists without an explicit mode."""
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw, parse_constant=_reject_non_json_number)
        if (not isinstance(value, dict) or not isinstance(value.get("cidrs"), list)
                or not isinstance(value.get("mode", "api_and_ui"), str)):
            return None
        if not isinstance(value.get("enabled"), bool):
            # Middleware enforces a nonempty list for any truthy enabled value.
            if not (value.get("enabled") and value["cidrs"]):
                return None
            value["enabled"] = True
        for network in value["cidrs"]:
            if not isinstance(network, str):
                return None
            ipaddress.ip_network(network, strict=False)
        value.setdefault("mode", "api_and_ui")
        return value
    except (ValueError, RecursionError):
        return None


def _run(db, disable):
    # Lock before reading on a recovery so a concurrent Settings save or second
    # command cannot overwrite a newer list or record the same recovery twice.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        query = "SELECT CAST(value AS TEXT) FROM system_config WHERE key = 'ip_allowlist'"
        if disable:
            query += " FOR UPDATE"
    else:
        if disable:
            db.execute(text("BEGIN IMMEDIATE"))
        query = "SELECT value FROM system_config WHERE key = 'ip_allowlist'"
    row = db.execute(text(query)).first()
    if row is None:
        return "No allowlist is stored."

    policy = _policy(row[0])
    if not disable:
        if policy is None:
            return "The stored allowlist cannot be read."
        state = "enabled" if policy["enabled"] else "disabled"
        return (f"The IP allowlist is {state}.\nMode: {json.dumps(policy['mode'])}\n"
                f"Networks: {json.dumps(policy['cidrs'])}")
    if policy is not None and not policy["enabled"]:
        return "The IP allowlist is already disabled."

    details = {"source": "from the host command"}
    if policy is None:
        policy = {"enabled": False, "cidrs": [], "mode": "api_and_ui"}
        details["stored_value_unreadable"] = True
        message = "The IP allowlist is disabled. The old list could not be kept."
    else:
        details.update(previous_enabled=policy["enabled"], network_count=len(policy["cidrs"]))
        policy["enabled"] = False
        message = "The IP allowlist is disabled. Your networks and mode were kept."

    ttl = cache_ttl_seconds()
    seconds = int(ttl) if ttl.is_integer() else ttl
    delay = f"within {seconds} {'second' if ttl == 1 else 'seconds'}" if ttl else "at once"
    db.execute(text("UPDATE system_config SET value = :value WHERE key = 'ip_allowlist'"),
               {"value": json.dumps(policy)})
    log_audit(db, "ip_allowlist_recovered", "system", details=details)
    return f"{message}\nRunning ODIN applies this {delay}."


def main(argv=None, *, session_factory=None):
    parser = argparse.ArgumentParser(prog="python -m modules.system.allowlist_recovery",
                                     description=__doc__, allow_abbrev=False)
    parser.add_argument("--disable", action="store_true", help="turn off the IP allowlist")
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return stop.code

    try:
        with (session_factory or _session)() as db, db.begin():
            message = _run(db, args.disable)
    except Exception:
        # Database errors can include connection details and bound values.
        print("ODIN could not read or save the IP allowlist.", file=sys.stderr)
        return 1
    print(message)
    if not args.disable:
        print("To disable the IP allowlist, run this command again with --disable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
