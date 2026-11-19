"""Real credential resolution must protect retries, including expiring tokens."""
import ast
import builtins
import json
from datetime import datetime, timedelta, timezone
from importlib.metadata import packages_distributions
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_api_token_delegation_behavior import (
    token_client, review_db, interactive_headers,
)


@pytest.fixture
def retry_client(token_client, review_db, monkeypatch):
    import core.db as db_mod
    import core.middleware.idempotency as idem
    from core.middleware.controlled_response import ControlledResponseMiddleware
    from core.rbac import require_any_scope
    from modules.system.routes_health import router

    client, issue = token_client
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: Session(review_db.get_bind()))
    monkeypatch.setattr(idem, "_SCHEMA_READY_CACHE", {"ready": False, "checked_at": 0.0})
    client.app.middleware("http")(idem.idempotency_middleware)
    client.app.add_middleware(ControlledResponseMiddleware)
    client.app.include_router(router, prefix="/api/v1")
    calls = []

    @client.app.patch("/api/v1/retry-test")
    def mutate(user=Depends(require_any_scope("agent:write"))):
        calls.append(user["id"])
        return {"writes": len(calls)}

    return client, issue, calls


def no_dateutil(monkeypatch):
    original = builtins.__import__

    def restricted(name, *args, **kwargs):
        if name.split(".")[0] == "dateutil":
            raise ModuleNotFoundError("dateutil is absent from the released image")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", restricted)


@pytest.mark.parametrize("carrier", ["X-API-Key", "Bearer"])
@pytest.mark.parametrize("shape", ["sqlite_naive", "sqlite_aware", "datetime_naive", "datetime_aware"])
def test_expiring_token_replays_without_dateutil(retry_client, review_db, monkeypatch, shape, carrier):
    import core.db as db_mod

    client, issue, calls = retry_client
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    if shape.endswith("naive"):
        expiry = expiry.replace(tzinfo=None)
    headers = {**issue(["agent:write"], expiry.isoformat(sep=" ")), "Idempotency-Key": "expiry-retry"}
    if carrier == "Bearer":
        headers["Authorization"] = "Bearer " + headers.pop("X-API-Key")
    if shape.startswith("datetime"):
        # PostgreSQL returns native datetimes for expires_at; keep the actual
        # SQLite queries and credentials, adapting only the returned value.
        class DatetimeSession(Session):
            def execute(self, statement, *args, **kwargs):
                result = super().execute(statement, *args, **kwargs)
                if "FROM api_tokens WHERE token_prefix" in str(statement) and "SELECT *" in str(statement):
                    rows = [SimpleNamespace(**{**row._mapping, "expires_at": expiry}) for row in result.fetchall()]
                    return SimpleNamespace(fetchall=lambda: rows)
                return result
        monkeypatch.setattr(db_mod, "SessionLocal", lambda: DatetimeSession(review_db.get_bind()))
    no_dateutil(monkeypatch)
    first = client.patch("/api/v1/retry-test", headers=headers)
    second = client.patch("/api/v1/retry-test", headers=headers)
    assert first.status_code == second.status_code == 200
    assert second.headers.get("X-Idempotent-Replay") == "true"
    assert first.json() == second.json() == {"writes": 1}
    assert calls == [3]
    with Session(review_db.get_bind()) as db:
        assert db.execute(text("SELECT COUNT(*) FROM idempotency_keys")).scalar_one() == 1


def test_inactive_owner_cannot_replay(retry_client, review_db):
    client, issue, calls = retry_client
    headers = {**issue(["agent:write"]), "Idempotency-Key": "inactive-owner"}
    assert client.patch("/api/v1/retry-test", headers=headers).status_code == 200
    review_db.execute(text("UPDATE users SET is_active=0 WHERE id=3"))
    review_db.commit()
    assert client.patch("/api/v1/retry-test", headers=headers).status_code == 401
    assert calls == [3]


@pytest.mark.parametrize("expired", [False, True])
def test_mixed_credentials_replay_for_the_route_principal(retry_client, expired):
    from core.auth import create_access_token

    client, issue, calls = retry_client
    lifetime = timedelta(minutes=-1) if expired else timedelta(hours=1)
    token = create_access_token({"sub": "student", "role": "viewer"}, expires_delta=lifetime)
    headers = {**issue(["agent:write"]), "Authorization": "Bearer " + token, "Idempotency-Key": "mixed"}
    path = "/api/v1/retry-test"
    assert client.patch(path, headers=headers).status_code == 200
    replay = client.patch(path, headers=headers)
    assert replay.status_code == 200 and replay.headers.get("X-Idempotent-Replay") == "true"
    assert calls == [3 if expired else 1]


@pytest.mark.parametrize("restricted_scope,expected", [("read:cameras", 401), ("agent:read", 409)])
def test_retry_after_scope_reduction(retry_client, review_db, restricted_scope, expected):
    client, issue, calls = retry_client
    headers = {**issue(["agent:write"]), "Idempotency-Key": "scope-change"}
    path = "/api/v1/retry-test"
    assert client.patch(path, headers=headers).status_code == 200
    review_db.execute(text("UPDATE api_tokens SET scopes=:s"), {"s": json.dumps([restricted_scope])})
    review_db.commit()
    retry = client.patch(path, headers=headers)
    assert retry.status_code == expected
    if expected == 409:
        assert retry.json()["error"]["code"] == "idempotency_authz_changed"
        fresh = client.patch(path, headers={**headers, "Idempotency-Key": "new-scope"})
        assert fresh.status_code == 403
    assert calls == [3]


def test_license_lock_refuses_cached_write(retry_client, monkeypatch):
    from core import license_lock

    client, issue, calls = retry_client
    headers = {**issue(["agent:write"]), "Idempotency-Key": "license-change"}
    path = "/api/v1/retry-test"
    assert client.patch(path, headers=headers).status_code == 200
    monkeypatch.setenv("ODIN_LICENSE_FLEET_LOCK_ENABLED", "1")
    locked = SimpleNamespace(revision=2, enforcing=True, users=frozenset(), entitlement={})
    monkeypatch.setattr(license_lock, "snapshot", lambda db, request: locked)
    for body in (None, {"changed": True}):
        retry = client.patch(path, headers=headers, json=body)
        assert retry.status_code == 403
        assert retry.headers["X-ODIN-License-Lock"] == "license_user_locked"
    assert calls == [3]


@pytest.mark.parametrize("expiry", ["2000-01-01 00:00:00", "not-a-date"])
def test_invalid_expiry_passes_to_route_auth(retry_client, review_db, expiry):
    client, issue, calls = retry_client
    headers = {**issue(["agent:write"], expiry), "Idempotency-Key": "invalid-expiry"}
    response = client.patch("/api/v1/retry-test", headers=headers)
    assert response.status_code == 401
    assert calls == []
    with Session(review_db.get_bind()) as db:
        assert db.execute(text("SELECT COUNT(*) FROM idempotency_keys")).scalar_one() == 0


@pytest.mark.parametrize("failure", ["token_lookup", "user_context", "jwt_lookup", "jwt_decode", "scope_decode", "resolver"])
def test_internal_resolution_failure_refuses_write(retry_client, review_db, monkeypatch, caplog, failure):
    import core.dependencies as auth
    import core.db as db_mod
    import core.middleware.idempotency as idem

    client, issue, calls = retry_client
    headers = interactive_headers() if failure.startswith("jwt") else issue(["agent:write"])
    headers["Idempotency-Key"] = "resolver-failure"
    marker = "synthetic-sensitive-exception-message"

    def broken(*args, **kwargs):
        raise RuntimeError(marker)

    needles = {"token_lookup": "SELECT * FROM api_tokens",
               "user_context": "SELECT * FROM users", "jwt_lookup": "FROM token_blacklist"}
    if failure in needles:
        class BrokenSession(Session):
            def execute(self, statement, *args, **kwargs):
                if needles[failure] in str(statement):
                    broken()
                return super().execute(statement, *args, **kwargs)
        monkeypatch.setattr(db_mod, "SessionLocal", lambda: BrokenSession(review_db.get_bind()))
    elif failure == "jwt_decode":
        monkeypatch.setattr(auth, "decode_token", broken)
    elif failure == "scope_decode":
        original = json.loads
        def load(value, *args, **kwargs):
            if value == '["agent:write"]':
                broken()
            return original(value, *args, **kwargs)
        monkeypatch.setattr(json, "loads", load)
    else:
        monkeypatch.setattr(idem, "_resolve_user_context", broken)
    # Returning the 503, rather than letting an exception escape, is part of the contract.
    response = client.patch("/api/v1/retry-test", headers=headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "idempotency_unavailable"
    assert response.json()["error"]["retriable"] is True
    assert calls == []
    assert ("HTTPException" if failure == "jwt_lookup" else "RuntimeError") in caplog.text
    assert marker not in caplog.text + response.text
    assert all(value not in caplog.text + response.text for key, value in headers.items() if key != "Idempotency-Key")


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid-test-jwt"}, {"X-API-Key": "odin_invalid-test-token"}])
def test_invalid_credentials_still_reach_route_auth(retry_client, headers):
    client, _, calls = retry_client
    response = client.patch("/api/v1/retry-test", headers={**headers, "Idempotency-Key": "invalid-auth"})
    assert response.status_code == 401
    assert calls == []


def test_middleware_imports_only_declared_dependencies():
    """Check lazy imports too; locally installed transitive deps are not enough."""
    import re
    import sys
    backend = Path(__file__).resolve().parents[2] / "backend"
    normalize = lambda value: re.sub(r"[-_.]+", "-", value).lower()
    declared = {normalize(line.split("==")[0].split("[")[0])
                for line in (backend / "requirements.txt").read_text().splitlines()
                if line.strip() and not line.startswith("#")}
    distributions = packages_distributions()
    local = {path.stem for path in backend.iterdir()}
    for path in (backend / "core" / "middleware").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root in sys.stdlib_module_names or root in local:
                    continue
                assert any(normalize(dist) in declared for dist in distributions.get(root, [])), f"{path.name}:{node.lineno} undeclared import {name}"


@pytest.mark.parametrize("scope,expected", [("agent:read", 200), ("agent:write", 200), ("read", 200), ("admin", 200), ("write:jobs", 403)])
def test_version_scopes(retry_client, scope, expected):
    client, issue, _ = retry_client
    response = client.get("/api/v1/version", headers=issue([scope]))
    assert response.status_code == expected
    if expected == 200:
        from modules.system.routes_health import __version__
        assert response.json() == {"version": __version__}


def test_version_remains_public_without_credentials(retry_client):
    client, _, _ = retry_client
    assert client.get("/api/v1/version").status_code == 200
