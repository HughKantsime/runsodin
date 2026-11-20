"""Personal API tokens through the real route dependencies and configured perimeter."""
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


def bearer(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def api(token_client, review_db, monkeypatch):
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from core.rate_limit import limiter
    from core import db
    from core.app import _register_http_middleware
    from core.config import settings
    from modules.organizations import routes_auth, routes_sessions

    client, issue = token_client
    client.app.state.limiter = limiter
    client.app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    key = "odin_" + secrets.token_urlsafe(24)
    monkeypatch.setenv("API_KEY", key)
    monkeypatch.setattr(settings, "api_key", key)
    monkeypatch.setattr(db, "SessionLocal", lambda: Session(review_db.get_bind()))
    client.app.include_router(routes_sessions.router, prefix="/api/v1")
    client.app.include_router(routes_auth.router, prefix="/api/v1")
    _register_http_middleware(client.app)
    return client, issue, key


def test_personal_token_reads_and_writes_with_its_scopes(api):
    client, issue, _ = api
    headers = bearer(issue(["read", "write"])["X-API-Key"])
    assert client.get("/api/v1/sessions", headers=headers).status_code == 200
    theme = {"mode": "dark"}
    response = client.put("/api/v1/auth/me/theme", headers=headers, json=theme)
    assert response.status_code == 200 and response.json() == theme
    assert client.get("/api/v1/auth/me/theme", headers=headers).json()["mode"] == "dark"
    assert client.post("/api/v1/auth/ws-token", headers=headers).status_code == 200
    assert client.delete("/api/v1/sessions", headers=headers).status_code == 403

    read = issue(["read"])
    responses = [client.put("/api/v1/auth/me/theme", headers=header, json={"mode": "light"})
                 for header in (read, bearer(read["X-API-Key"]))]
    assert [response.status_code for response in responses] == [403, 403]
    assert responses[0].json() == responses[1].json()


@pytest.mark.parametrize("case", ["expired", "revoked", "camera-only", "unknown"])
def test_unusable_bearer_does_not_authenticate(api, review_db, case):
    client, issue, _ = api
    expiry = "2000-01-01 00:00:00" if case == "expired" else None
    token = issue(["read:cameras"] if case == "camera-only" else ["read"], expiry)["X-API-Key"]
    if case == "revoked":
        assert client.get("/api/v1/sessions", headers=bearer(token)).status_code == 200
        review_db.execute(text("DELETE FROM api_tokens WHERE token_prefix=:prefix"), {"prefix": token[:10]})
        review_db.commit()
    elif case == "unknown":
        token += "-invalid"
    status = client.get("/api/v1/sessions", headers=bearer(token)).status_code
    assert status == 401


def test_precedence_and_legacy_key(api):
    from core.auth import create_access_token

    client, issue, key = api
    viewer = create_access_token({"sub": "student", "role": "viewer"})
    assert client.get("/api/v1/sessions", headers=bearer(viewer)).status_code == 200
    path = "/api/v1/auth/me"
    read = issue(["read"])
    write = bearer(issue(["write"])["X-API-Key"])
    assert client.put("/api/v1/auth/me/theme", headers={**read, **write}, json={}).status_code == 403
    assert client.get(path, headers={**read, **bearer(viewer)}).json()["username"] == "student"
    assert client.get(path, headers={**read, **write, "Cookie": "session=" + viewer}).json()["username"] == "student"
    assert client.get(path, headers={"X-API-Key": key, **bearer(viewer)}).json()["username"] == "student"
    assert client.get(path, headers={"X-API-Key": key}).json()["username"] == "admin"
    assert client.get("/api/v1/sessions", headers=bearer(key)).status_code == 401
    assert client.get(path, headers={"X-API-Key": "odin_invalid", **write}).status_code == 401


def test_nonprefixed_legacy_key_requires_x_api_key(api, monkeypatch):
    from core.config import settings

    client, _, _ = api
    key = "legacy-" + secrets.token_hex(24)
    monkeypatch.setenv("API_KEY", key)
    monkeypatch.setattr(settings, "api_key", key)
    for path in ("/api/v1/sessions", "/api/v1/auth/me"):
        assert client.get(path, headers={"X-API-Key": key}).status_code == 200
        assert client.get(path, headers=bearer(key)).status_code == 401


@pytest.mark.parametrize("perimeter", [False, True])
@pytest.mark.parametrize("companion", [None, "", "undefined", "garbage", "personal"])
def test_personal_bearer_selects_token_owner(api, review_db, monkeypatch, perimeter, companion):
    from core.config import settings

    client, issue, _ = api
    if not perimeter:
        monkeypatch.delenv("API_KEY")
        monkeypatch.setattr(settings, "api_key", "")
    headers = bearer(issue(["read"])["X-API-Key"])
    review_db.execute(text("UPDATE api_tokens SET user_id=1"))
    review_db.execute(text("INSERT INTO active_sessions(user_id,token_jti,user_agent) VALUES "
                           "(1,'student-session','student'),(3,'admin-session','admin')"))
    review_db.commit()
    if companion == "personal":
        headers.update(issue(["read"]))
    elif companion is not None:
        headers["X-API-Key"] = companion
    response = client.get("/api/v1/sessions", headers=headers)
    if perimeter and companion in {"undefined", "garbage"}:
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or missing API key"
    else:
        assert response.status_code == 200
        assert [s["user_agent"] for s in response.json()] == ["admin" if companion == "personal" else "student"]


@pytest.mark.parametrize("carrier", ["cookie", "personal"])
def test_login_with_locked_caller_reaches_handler_with_retry_key(api, review_db, monkeypatch, carrier):
    from types import SimpleNamespace
    from core import license_lock
    from core.auth import create_access_token

    client, issue, _ = api
    headers = ({"Cookie": "session=" + create_access_token({"sub": "admin", "role": "admin"})}
               if carrier == "cookie" else bearer(issue(["write"])["X-API-Key"]))
    monkeypatch.setenv("ODIN_LICENSE_FLEET_LOCK_ENABLED", "1")
    locked = SimpleNamespace(revision=2, enforcing=True, users=set(), entitlement={})
    monkeypatch.setattr(license_lock, "snapshot", lambda db, request: locked)
    form = {"username": "missing-login-user", "password": "unused"}
    for extra in ({}, {"Idempotency-Key": "locked-login"}):
        response = client.post("/api/v1/auth/login", data=form, headers={**headers, **extra})
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid credentials"
    assert review_db.scalar(text("SELECT COUNT(*) FROM login_attempts WHERE username='missing-login-user'")) == 2
    assert review_db.scalar(text("SELECT COUNT(*) FROM idempotency_keys")) == 0


@pytest.mark.parametrize("perimeter", [False, True])
@pytest.mark.parametrize("companion", ["absent", "undefined", "wrong", "personal", "perimeter"])
def test_bearer_jwt_owns_session_with_companion_key(api, review_db, monkeypatch, perimeter, companion):
    from core.auth import create_access_token
    from core.config import settings
    import jwt

    client, issue, key = api
    if not perimeter:
        monkeypatch.delenv("API_KEY")
        monkeypatch.setattr(settings, "api_key", "")
    token = create_access_token({"sub": "student", "role": "viewer"})
    jti = jwt.decode(token, options={"verify_signature": False})["jti"]
    review_db.execute(text("INSERT INTO active_sessions(user_id,token_jti) VALUES (1,:jti)"), {"jti": jti})
    review_db.commit()
    headers = bearer(token)
    if companion == "personal":
        headers.update(issue(["read"]))  # Owned by the admin.
    elif companion != "absent":
        headers["X-API-Key"] = key if companion == "perimeter" else companion

    # /auth/me exercises identity even on deployments where the perimeter
    # rejects a bad companion key on non-exempt endpoints.
    identity = client.get("/api/v1/auth/me", headers=headers)
    assert identity.status_code == 200
    assert identity.json()["username"] == "student"
    sessions = client.get("/api/v1/sessions", headers=headers)
    assert sessions.status_code == (401 if perimeter and companion in {"undefined", "wrong"} else 200)
    if sessions.status_code == 200:
        assert [s["is_current"] for s in sessions.json()] == [True]


def test_mixed_headers_use_jwt_for_licensed_session_actions(api, monkeypatch):
    from types import SimpleNamespace
    from core import license_lock
    from core.auth import create_access_token

    client, issue, _ = api
    session = bearer(create_access_token({"sub": "student", "role": "viewer"}))
    headers = {**session, **issue(["read"])}
    monkeypatch.setenv("ODIN_LICENSE_FLEET_LOCK_ENABLED", "1")
    policy = SimpleNamespace(revision=1, enforcing=True, users={1}, entitlement={})
    monkeypatch.setattr(license_lock, "snapshot", lambda db, request: policy)

    # The JWT user has a seat. The companion token's owner does not.
    assert client.delete("/api/v1/sessions", headers=headers).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me", headers=session).status_code == 401


def test_expiring_bearer_replays_only_while_authorized(api, review_db):
    client, issue, _ = api
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    token = issue(["write"], expiry)["X-API-Key"]
    retry = {"Idempotency-Key": "theme-retry"}
    path = "/api/v1/auth/me/theme"

    def change(header):
        return client.put(path, headers={**retry, **header}, json={"mode": "dark"})

    assert change(bearer(token)).status_code == 200
    for header in (bearer(token), {"X-API-Key": token}):
        response = change(header)
        assert response.status_code == 200 and response.headers["X-Idempotent-Replay"] == "true"
    review_db.execute(text("UPDATE api_tokens SET expires_at='2000-01-01' WHERE token_prefix=:prefix"),
                      {"prefix": token[:10]})
    review_db.commit()
    assert change(bearer(token)).status_code == 401
    assert review_db.execute(text("SELECT user_id FROM idempotency_keys")).scalars().all() == [3]


def test_ws_ticket_rate_limit_and_login_attempts_are_shared_across_headers(api, review_db):
    from unittest.mock import patch
    from core import dependencies

    client, issue, _ = api
    path = "/api/v1/auth/ws-token"
    read = issue(["read"])
    invalid = ({"X-API-Key": "odin_unknown"}, bearer("odin_unknown"))
    with patch.object(dependencies, "resolve_user_api_token", wraps=dependencies.resolve_user_api_token) as lookup:
        for header in invalid:
            lookup.reset_mock()
            assert client.post(path, headers=header).status_code == 401
            assert lookup.call_count == 1
            assert lookup.call_args.args[0] == "odin_unknown"
    assert review_db.execute(text("SELECT COUNT(*) FROM login_attempts")).scalar_one() == 0
    for header in (read, bearer(read["X-API-Key"])) * 5:
        assert client.post(path, headers=header).status_code == 200
    for header in (read, bearer(read["X-API-Key"])):
        assert client.post(path, headers=header).status_code == 429

    form = {"username": "unknown-ws-user", "password": "unused"}
    login = "/api/v1/auth/login"
    for attempt in range(5):
        assert client.post(login, data=form, headers=invalid[attempt % 2]).status_code == 401
    for header in invalid:
        assert client.post(login, data=form, headers=header).status_code == 423


def test_cookie_owns_retry_even_when_both_headers_carry_personal_tokens(api, review_db):
    from core.auth import create_access_token

    client, issue, _ = api
    cookie = {"Cookie": "session=" + create_access_token({"sub": "student", "role": "viewer"})}
    retry = {"Idempotency-Key": "mixed-owners"}
    headers = {**issue(["read"]), **bearer(issue(["write"])["X-API-Key"]), **cookie, **retry}
    path = "/api/v1/auth/me/theme"
    assert client.put(path, headers=headers, json={"mode": "dark"}).status_code == 200
    response = client.put(path, headers={**cookie, **retry}, json={"mode": "dark"})
    assert response.status_code == 200 and response.headers["X-Idempotent-Replay"] == "true"
    assert review_db.execute(text("SELECT user_id FROM idempotency_keys")).scalars().all() == [1]
