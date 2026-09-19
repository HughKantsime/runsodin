from __future__ import annotations

import sys
import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def oidc_db(tmp_path: Path):
    from core.schema import bootstrap_database

    engine = create_engine(f"sqlite:///{tmp_path / 'oidc.db'}")
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO groups (id, name, is_org) VALUES (1, 'school', 1)")
        )
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _config(**overrides) -> dict:
    return {
        "display_name": "Entra",
        "auto_create_users": True,
        "default_role": "viewer",
        "default_group_id": 1,
        **overrides,
    }


def _claims(issuer: str = "https://issuer.example", subject: str = "subject-1") -> dict:
    return {"iss": issuer, "sub": subject, "email": "student@example.test"}


def _seed_oidc_config(db: Session) -> None:
    db.execute(
        text(
            "INSERT INTO oidc_config "
            "(id, display_name, auto_create_users, default_role, default_group_id, is_enabled) "
            "VALUES (1, 'Entra', 1, 'viewer', 1, 1)"
        )
    )
    db.commit()


def _oidc_client(db: Session) -> TestClient:
    from core.db import get_db
    from modules.organizations.routes_oidc import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


class _CallbackHandler:
    def validate_state(self, state: str) -> bool:
        return state == "fixture-state"

    async def exchange_code(self, code: str) -> dict:
        assert code == "fixture-code"
        return {"id_token": "fixture-id-token", "access_token": "fixture-access-token"}

    async def parse_id_token(self, token: str) -> dict:
        assert token == "fixture-id-token"
        return _claims()

    async def get_user_info(self, token: str) -> dict:
        assert token == "fixture-access-token"
        return {}


def test_oidc_auto_create_is_viewer_in_explicit_tenant(oidc_db: Session) -> None:
    from modules.organizations.routes_oidc import _resolve_oidc_user

    user = _resolve_oidc_user(oidc_db, _config(), _claims(), {})
    assert user["role"] == "viewer"
    assert user["group_id"] == 1
    stored = oidc_db.execute(
        text(
            "SELECT role, group_id, oidc_issuer, oidc_subject FROM users WHERE id=:id"
        ),
        {"id": user["id"]},
    ).one()
    assert tuple(stored) == (
        "viewer",
        1,
        "https://issuer.example",
        "subject-1",
    )


def test_same_subject_at_different_exact_issuer_is_a_distinct_identity(
    oidc_db: Session,
) -> None:
    from modules.organizations.routes_oidc import _resolve_oidc_user

    first = _resolve_oidc_user(oidc_db, _config(), _claims("https://issuer-a"), {})
    second = _resolve_oidc_user(oidc_db, _config(), _claims("https://issuer-b"), {})
    assert first["id"] != second["id"]


def test_legacy_identity_binds_once_and_ambiguity_fails_closed(
    oidc_db: Session,
) -> None:
    from modules.organizations.routes_oidc import OIDCIdentityError, _resolve_oidc_user

    oidc_db.execute(
        text(
            "INSERT INTO users "
            "(username, email, password_hash, role, is_active, group_id, oidc_subject, oidc_provider) "
            "VALUES ('legacy', 'legacy@example.test', 'fixture', 'viewer', 1, 1, 'legacy-sub', 'entra')"
        )
    )
    oidc_db.commit()
    bound = _resolve_oidc_user(
        oidc_db,
        _config(),
        _claims("https://issuer.example", "legacy-sub"),
        {},
    )
    assert bound["username"] == "legacy"
    assert bound["oidc_issuer"] == "https://issuer.example"

    oidc_db.execute(
        text(
            "INSERT INTO users "
            "(username, email, password_hash, role, is_active, group_id, oidc_subject, oidc_provider) "
            "VALUES ('ambiguous-1', 'a1@example.test', 'fixture', 'viewer', 1, 1, 'dup-sub', 'entra'), "
            "('ambiguous-2', 'a2@example.test', 'fixture', 'viewer', 1, 1, 'dup-sub', 'entra')"
        )
    )
    oidc_db.commit()
    with pytest.raises(OIDCIdentityError) as exc:
        _resolve_oidc_user(
            oidc_db,
            _config(),
            _claims("https://issuer.example", "dup-sub"),
            {},
        )
    assert exc.value.code == "oidc_identity_ambiguous"


@pytest.mark.parametrize(
    ("config", "code"),
    [
        (_config(default_group_id=None), "oidc_default_tenant_required"),
        (_config(default_role="operator"), "oidc_role_configuration_invalid"),
    ],
)
def test_oidc_auto_create_configuration_fails_closed(
    oidc_db: Session, config: dict, code: str
) -> None:
    from modules.organizations.routes_oidc import OIDCIdentityError, _resolve_oidc_user

    with pytest.raises(OIDCIdentityError) as exc:
        _resolve_oidc_user(oidc_db, config, _claims(), {})
    assert exc.value.code == code


def test_inactive_exact_oidc_identity_is_rejected(oidc_db: Session) -> None:
    from modules.organizations.routes_oidc import OIDCIdentityError, _resolve_oidc_user

    oidc_db.execute(
        text(
            "INSERT INTO users "
            "(username, email, password_hash, role, is_active, group_id, oidc_issuer, oidc_subject) "
            "VALUES ('inactive', 'inactive@example.test', 'fixture', 'viewer', 0, 1, "
            "'https://issuer.example', 'subject-1')"
        )
    )
    oidc_db.commit()
    with pytest.raises(OIDCIdentityError) as exc:
        _resolve_oidc_user(oidc_db, _config(), _claims(), {})
    assert exc.value.code == "user_inactive"


def test_oidc_identity_creation_rolls_back_with_failed_callback_transaction(
    oidc_db: Session,
) -> None:
    from modules.organizations.routes_oidc import _resolve_oidc_user

    created = _resolve_oidc_user(oidc_db, _config(), _claims(), {})
    observer = Session(oidc_db.get_bind())
    try:
        assert observer.execute(
            text("SELECT COUNT(*) FROM users WHERE id=:id"), {"id": created["id"]}
        ).scalar_one() == 0
        oidc_db.rollback()
        assert observer.execute(
            text("SELECT COUNT(*) FROM users WHERE id=:id"), {"id": created["id"]}
        ).scalar_one() == 0
    finally:
        observer.close()


def test_oidc_callback_commits_identity_and_auth_code_together(
    oidc_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core import crypto
    from modules.organizations import oidc_handler

    _seed_oidc_config(oidc_db)
    monkeypatch.setattr(
        oidc_handler,
        "create_handler_from_config",
        lambda config, redirect_uri: _CallbackHandler(),
    )
    monkeypatch.setattr(crypto, "encrypt", lambda value: f"encrypted:{value}")
    response = _oidc_client(oidc_db).get(
        "/auth/oidc/callback?code=fixture-code&state=fixture-state",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith("/?oidc_code=")
    assert oidc_db.execute(
        text("SELECT COUNT(*) FROM users WHERE oidc_subject='subject-1'")
    ).scalar_one() == 1
    assert oidc_db.execute(
        text("SELECT COUNT(*) FROM oidc_auth_codes")
    ).scalar_one() == 1


def test_oidc_callback_auth_code_failure_rolls_back_new_identity(
    oidc_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core import crypto
    from modules.organizations import oidc_handler

    _seed_oidc_config(oidc_db)
    monkeypatch.setattr(
        oidc_handler,
        "create_handler_from_config",
        lambda config, redirect_uri: _CallbackHandler(),
    )
    monkeypatch.setattr(crypto, "encrypt", lambda value: f"encrypted:{value}")
    engine = oidc_db.get_bind()

    def fail_auth_code_insert(
        connection, cursor, statement, parameters, context, executemany
    ):
        if statement.lstrip().upper().startswith("INSERT INTO OIDC_AUTH_CODES"):
            raise RuntimeError("synthetic auth-code issuance failure")
        return statement, parameters

    event.listen(engine, "before_cursor_execute", fail_auth_code_insert, retval=True)
    try:
        response = _oidc_client(oidc_db).get(
            "/auth/oidc/callback?code=fixture-code&state=fixture-state",
            follow_redirects=False,
        )
    finally:
        event.remove(engine, "before_cursor_execute", fail_auth_code_insert)
    assert response.status_code == 302
    assert response.headers["location"] == "/?error=auth_failed"
    assert oidc_db.execute(
        text("SELECT COUNT(*) FROM users WHERE oidc_subject='subject-1'")
    ).scalar_one() == 0
    assert oidc_db.execute(
        text("SELECT COUNT(*) FROM oidc_auth_codes")
    ).scalar_one() == 0


def test_oidc_admin_cannot_clear_tenant_while_auto_create_stays_enabled(
    oidc_db: Session,
) -> None:
    from modules.organizations.routes_oidc import update_oidc_config

    _seed_oidc_config(oidc_db)
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {
            "type": "http.request",
            "body": b'{"default_group_id":null}',
            "more_body": False,
        }

    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/admin/oidc",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )
    with pytest.raises(HTTPException) as rejected:
        asyncio.run(update_oidc_config(request, {"id": 99, "role": "admin"}, oidc_db))
    assert rejected.value.status_code == 422
    assert oidc_db.execute(
        text("SELECT default_group_id FROM oidc_config WHERE id=1")
    ).scalar_one() == 1


def test_id_token_parser_requires_exact_issuer_and_standard_subject() -> None:
    source = (
        BACKEND / "modules" / "organizations" / "oidc_handler.py"
    ).read_text(encoding="utf-8")
    assert "issuer=issuer" in source
    assert '"verify_iss": True' in source
    assert '"require": ["exp", "aud", "iss", "sub"]' in source
    callback = (
        BACKEND / "modules" / "organizations" / "routes_oidc.py"
    ).read_text(encoding="utf-8")
    assert 'claims.get("sub")' in callback
    assert 'claims.get("oid")' not in callback


def test_id_token_parser_rejects_wrong_issuer_and_missing_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    from core import itar
    from modules.organizations import oidc_handler
    from modules.organizations.oidc_handler import OIDCHandler

    issuer = "https://issuer.example/exact"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk["kid"] = "fixture-key"

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": [jwk]}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url: str, timeout: int):
            return _Response()

    monkeypatch.setattr(oidc_handler.httpx, "AsyncClient", lambda **kwargs: _Client())
    monkeypatch.setattr(itar, "pin_for_request", lambda url: nullcontext())
    handler = OIDCHandler("client-id", "secret", "tenant", "https://callback")

    async def _config():
        return {"issuer": issuer, "jwks_uri": "https://issuer.example/jwks"}

    monkeypatch.setattr(handler, "_get_oidc_config", _config)
    now = datetime.now(timezone.utc)
    base = {
        "aud": "client-id",
        "iss": issuer,
        "sub": "student-1",
        "exp": now + timedelta(minutes=5),
    }
    valid = jwt.encode(base, private_key, algorithm="RS256", headers={"kid": "fixture-key"})
    assert asyncio.run(handler.parse_id_token(valid))["sub"] == "student-1"

    wrong_issuer = jwt.encode(
        {**base, "iss": "https://issuer.example/other"},
        private_key,
        algorithm="RS256",
        headers={"kid": "fixture-key"},
    )
    with pytest.raises(ValueError, match="ID token validation failed"):
        asyncio.run(handler.parse_id_token(wrong_issuer))

    without_subject = jwt.encode(
        {key: value for key, value in base.items() if key != "sub"},
        private_key,
        algorithm="RS256",
        headers={"kid": "fixture-key"},
    )
    with pytest.raises(ValueError, match="ID token validation failed"):
        asyncio.run(handler.parse_id_token(without_subject))
