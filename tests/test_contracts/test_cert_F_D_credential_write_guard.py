"""Runtime credential-write guard (security re-review round 3, MEDIUM).

The static census can be bypassed by code shape. These tests register
throwaway writers in every shape the reviewer used and prove the runtime guard
refuses them for an API-token request while a login session is unaffected.
"""
from __future__ import annotations

import contextvars
import os

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, String, create_engine, text, update
from sqlalchemy.orm import Session, declarative_base

from core.credential_write_guard import (
    CREDENTIAL_WRITE_DETAIL, guarded_write_target, install_credential_write_guard,
    mark_noninteractive_principal, request_is_noninteractive,
)

install_credential_write_guard()

_Base = declarative_base()


class _User(_Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String)
    email = Column(String)
    theme = Column(String)


class _Push(_Base):
    __tablename__ = "push_subscriptions"
    id = Column(Integer, primary_key=True)
    endpoint = Column(String)


EMAIL_SQL = "UPDATE users SET email = :e WHERE id = 1"  # constant SQL


def _deep_helper(db):  # a helper two calls deep
    _deeper(db)


def _deeper(db):
    table = "users"
    db.execute(text(f"UPDATE {table} SET email = :e WHERE id = 1"), {"e": "f@x.test"})  # f-string


WRITERS = {
    "constant_sql": lambda db: db.execute(text(EMAIL_SQL), {"e": "c@x.test"}),
    "fstring_helper_two_deep": _deep_helper,
    "orm_attribute": lambda db: setattr(db.get(_User, 1), "email", "o@x.test"),
    "orm_setattr_password_shape": lambda db: setattr(db.get(_User, 1), "username", "renamed"),
    "orm_add": lambda db: db.add(_Push(endpoint="https://attacker.example.test/push")),
    "orm_update_statement": lambda db: db.execute(update(_User).where(_User.id == 1).values(email="u@x.test")),
    "config_upsert": lambda db: db.execute(text(
        "INSERT INTO system_config (key, value) VALUES ('smtp_config', :v) "
        "ON CONFLICT(key) DO UPDATE SET value = :v"), {"v": "{}"}),
    "config_key_param": lambda db: db.execute(text(
        "UPDATE system_config SET value = :v WHERE key = :k"), {"v": "http://evil", "k": "base_url"}),
}


@pytest.fixture
def app(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'guard.db'}")
    _Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE system_config (key TEXT PRIMARY KEY, value TEXT)"))
        c.execute(text("INSERT INTO system_config VALUES ('base_url', 'https://odin.test')"))
        c.execute(text("INSERT INTO users (id, username, email, theme) VALUES (1, 'victim', 'v@x.test', 'dark')"))

    def db():
        with Session(engine) as s:
            yield s

    async def token_principal():  # async, like get_current_user: marks the request task
        mark_noninteractive_principal()  # what get_current_user does for an API token
        return {"_auth_kind": "user_api_token"}

    def session_principal():
        return {"_auth_kind": "session_jwt"}

    api = FastAPI()
    for name, writer in WRITERS.items():
        def route(s: Session = Depends(db), _w=writer):
            _w(s)
            s.commit()
            return {"ok": True}
        api.add_api_route(f"/token/{name}", route, methods=["POST"], dependencies=[Depends(token_principal)])
        api.add_api_route(f"/session/{name}", route, methods=["POST"], dependencies=[Depends(session_principal)])

    def theme(s: Session = Depends(db)):  # a non-credential column stays writable
        s.execute(text("UPDATE users SET theme = 'light' WHERE id = 1"))
        s.commit()
        return {"ok": True}
    api.add_api_route("/token/theme", theme, methods=["POST"], dependencies=[Depends(token_principal)])
    return TestClient(api), engine


@pytest.mark.parametrize("shape", sorted(WRITERS))
def test_token_request_cannot_write_credentials_in_any_code_shape(app, shape):
    client, engine = app
    before = _snapshot(engine)
    r = client.post(f"/token/{shape}")
    assert r.status_code == 403, (shape, r.status_code, r.text)
    assert r.json()["detail"] == CREDENTIAL_WRITE_DETAIL
    assert _snapshot(engine) == before  # nothing reached the database


@pytest.mark.parametrize("shape", sorted(WRITERS))
def test_login_session_request_is_unaffected(app, shape):
    client, _ = app
    assert client.post(f"/session/{shape}").status_code == 200, shape


def test_token_request_can_still_write_non_credential_columns(app):
    client, engine = app
    assert client.post("/token/theme").status_code == 200
    with engine.connect() as c:
        assert c.execute(text("SELECT theme FROM users WHERE id = 1")).scalar_one() == "light"


def test_mark_does_not_leak_between_requests(app):
    client, _ = app
    assert client.post("/token/constant_sql").status_code == 403
    assert client.post("/session/constant_sql").status_code == 200


def test_background_work_outside_a_request_is_never_marked():
    assert contextvars.copy_context().run(request_is_noninteractive) is False


def _snapshot(engine):
    with engine.connect() as c:
        return (c.execute(text("SELECT username, email FROM users")).all(),
                c.execute(text("SELECT key, value FROM system_config ORDER BY key")).all(),
                c.execute(text("SELECT COUNT(*) FROM push_subscriptions")).scalar_one())


@pytest.mark.parametrize("sql,params,expected", [
    ("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", None, None),  # auth telemetry
    ("UPDATE api_tokens SET scopes = ? WHERE id = ?", None, "api_tokens"),
    ("UPDATE users SET role = ?, is_active = ? WHERE id = ?", None, None),
    ('UPDATE "users" AS u SET "password_hash" = ? WHERE id = 1', None, "users"),
    ("UPDATE users SET (email, role) = (?, ?)", None, "users"),
    ("INSERT INTO users (username, email) VALUES (?, ?)", None, "users"),
    ("INSERT INTO public.biometric_tokens VALUES (?)", None, "biometric_tokens"),
    ("DELETE FROM users WHERE id = ?", None, None),  # removing access is not a takeover
    ("DELETE FROM api_tokens WHERE user_id = ?", None, None),
    ("DELETE FROM oidc_config", None, "oidc_config"),
    ("DELETE FROM system_config WHERE key = ?", ("setup_complete",), "system_config"),
    ("UPDATE system_config SET value = ? WHERE key = ?", ("x", "oidc_client_secret"), "system_config"),
    ("UPDATE system_config SET value = ? WHERE key = ?", ("x", "quiet_hours"), None),
    ("/* c */ UPDATE -- x\n users SET email = ?", None, "users"),
    ("UPDATE printers SET name = ?", None, None),
    # Security re-review shapes.
    ("UPDATE api_tokens SET id=id WHERE id=:id", {"id": 1}, None),  # row-lock idiom
    ("UPDATE active_sessions SET id = id WHERE 0", None, None),
    ("UPDATE [users] SET email=?", None, "users"),
    ("UPDATE `users` SET email=?", None, "users"),
    ("UPDATE users SET role=(SELECT r FROM x WHERE y=1), email=:e WHERE id=1", {"e": 1}, "users"),
    ("UPDATE system_config SET value=? WHERE key LIKE 'oid%'", ("x",), "system_config"),
    ("DELETE FROM system_config WHERE key LIKE 'oid%'", None, "system_config"),
    ("UPDATE system_config SET value=? WHERE key='smtp_'||'config'", ("x",), "system_config"),
    ("UPDATE system_config SET value=:value WHERE key=:k", {"value": "oidc-blue", "k": "theme"}, None),
    ("UPDATE system_config SET value=:value WHERE key=:key", {"value": "x", "key": "oidc_client_secret"}, "system_config"),
    # PostgreSQL / psycopg rendering of the same statements.
    ("UPDATE users SET email=%(email)s WHERE users.id = %(id_1)s", {"email": "x", "id_1": 1}, "users"),
    ("UPDATE users SET theme=%(theme)s WHERE users.id = %(id_1)s", {"theme": "x", "id_1": 1}, None),
    ("UPDATE system_config SET value=%(value)s WHERE system_config.key = %(key_1)s",
     {"value": "x", "key_1": "base_url"}, "system_config"),
    ("INSERT INTO push_subscriptions (user_id, endpoint) VALUES (%(user_id)s, %(endpoint)s) RETURNING push_subscriptions.id",
     {"user_id": 1, "endpoint": "e"}, "push_subscriptions"),
])
def test_classifier(sql, params, expected):
    assert guarded_write_target(sql, params) == expected


def test_get_current_user_marks_token_principals(monkeypatch, tmp_path):
    """Wiring: a legacy global key resolves to a marked (non-interactive) context."""
    import asyncio
    from core.dependencies import get_current_user
    from core.schema import bootstrap_database
    from pathlib import Path
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    bootstrap_database(engine, Path(__file__).resolve().parents[2] / "backend")
    with engine.begin() as c:
        c.execute(text("INSERT INTO users (id,username,email,password_hash,role,is_active) "
                       "VALUES (1,'admin','a@x.test','fixture','admin',1)"))
    monkeypatch.setenv("API_KEY", "synthetic-global-key-" + "k" * 24)

    class Req:
        cookies = {}
        headers = {"X-API-Key": os.environ["API_KEY"]}
        url = type("U", (), {"path": "/api/x"})()
        method = "GET"
        scope = {}

    async def resolve():  # same task, as FastAPI runs async dependencies
        with Session(engine) as db:
            user = await get_current_user(Req(), token=None, db=db)
            return user, request_is_noninteractive()
    user, marked = asyncio.run(resolve())
    assert user and user["_auth_kind"] == "legacy_global_api_key"
    assert marked is True


def test_raw_dbapi_adapter_is_guarded_too(tmp_path, monkeypatch):
    """core.db_utils.get_db hands out raw DBAPI cursors that bypass SQLAlchemy events."""
    import asyncio
    from core import db_utils
    engine = create_engine(f"sqlite:///{tmp_path / 'raw.db'}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, theme TEXT)"))
        c.execute(text("INSERT INTO users VALUES (1, 'v@x.test', 'dark')"))
    monkeypatch.setattr(db_utils, "engine", engine)

    async def as_token_request():
        mark_noninteractive_principal()
        with db_utils.get_db() as conn:
            conn.execute("UPDATE users SET theme = ? WHERE id = 1", ("light",))
            with pytest.raises(Exception) as refused:
                conn.execute("UPDATE users SET email = ? WHERE id = 1", ("evil@x.test",))
            conn.commit()
        return refused.value
    refused = asyncio.run(as_token_request())
    assert getattr(refused, "status_code", None) == 403
    with engine.connect() as c:
        assert c.execute(text("SELECT email, theme FROM users")).one() == ("v@x.test", "light")
