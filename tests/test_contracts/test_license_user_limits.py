"""Signed nullable entitlements must produce usable, tier-aware user limits."""

import asyncio
import base64
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


@pytest.fixture
def signed_license(tmp_path, monkeypatch):
    import license_manager as lm

    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    path = tmp_path / "odin.license"
    monkeypatch.setattr(lm, "ODIN_PUBLIC_KEY", public_key)
    monkeypatch.setattr(lm, "_find_license_file", lambda: str(path))
    monkeypatch.delenv("ODIN_REQUIRE_LICENSE_BINDING", raising=False)

    def load(**overrides):
        payload = {
            "tier": "education", "licensee": "Test School",
            "expires_at": "2099-01-01", "features": ["education_workflows"],
            **overrides,
        }
        raw = json.dumps(payload).encode()
        path.write_text(
            base64.urlsafe_b64encode(raw).decode() + "."
            + base64.urlsafe_b64encode(key.sign(raw)).decode()
        )
        info = lm.load_license()
        monkeypatch.setattr(lm, "get_license", lambda: info)
        return lm, info

    return load


@pytest.mark.parametrize("tier", ["community", "pro", "education", "enterprise"])
@pytest.mark.parametrize("limits", [{}, {"max_users": None}])
def test_missing_or_null_uses_tier_default(signed_license, tier, limits):
    lm, info = signed_license(tier=tier, **limits)
    assert info.valid
    assert info.max_users == lm.TIERS[tier]["max_users"]
    assert info.to_public_dict()["max_users"] == info.max_users
    if info.max_users is None:
        lm.check_user_limit(10000)
        return
    lm.check_user_limit(info.max_users - 1)
    with pytest.raises(HTTPException) as exc:
        lm.check_user_limit(info.max_users)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("cap", [0, 2, 374])
def test_explicit_caps_are_preserved(signed_license, cap):
    lm, info = signed_license(max_users=cap)
    assert info.valid
    assert info.max_users == cap
    with pytest.raises(HTTPException) as exc:
        lm.check_user_limit(cap)
    assert exc.value.status_code == 403
    if cap:
        lm.check_user_limit(cap - 1)


def test_expired_license_keeps_community_limit(signed_license):
    lm, info = signed_license(max_users=None, expires_at="2000-01-01")
    assert not info.valid
    assert info.expired
    assert info.max_users == 1
    with pytest.raises(HTTPException):
        lm.check_user_limit(1)


def test_null_education_limit_reaches_public_api(signed_license, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from modules.system import routes_health

    _, info = signed_license(max_users=None)
    monkeypatch.setattr(routes_health, "get_license", lambda: info)
    app = FastAPI()
    app.add_api_route("/license", routes_health.get_license_info)
    response = TestClient(app).get("/license")
    assert response.status_code == 200
    assert response.json()["max_users"] is None  # O13: Education is unlimited
    assert "sso" in response.json()["features"]


def test_school_can_create_second_user_and_provision_oidc(signed_license, tmp_path, monkeypatch):
    from pathlib import Path
    from core.auth import UserCreate
    from core.schema import bootstrap_database
    from modules.organizations import routes_users
    from modules.organizations.routes_oidc import _resolve_oidc_user

    lm, _ = signed_license(max_users=None)
    # Other license tests reload the module; bind the real enforcement function
    # from this fixture's module rather than an earlier cached import.
    monkeypatch.setattr(routes_users, "check_user_limit", lm.check_user_limit)
    engine = create_engine(f"sqlite:///{tmp_path / 'users.db'}")
    bootstrap_database(engine, Path(__file__).resolve().parents[2] / "backend")
    try:
        with Session(engine) as db:
            db.execute(text("INSERT INTO groups (id, name, is_org) VALUES (1, 'School', 1)"))
            db.execute(text("INSERT INTO users (username, password_hash, role, group_id) VALUES ('admin', '', 'admin', 1)"))
            db.commit()
            result = asyncio.run(routes_users.create_user(
                UserCreate(username="teacher", email="teacher@example.test", password="TestPassword123!", role="viewer", group_id=1),
                current_user={"id": 1, "role": "admin", "group_id": 1}, db=db,
            ))
            assert result == {"status": "created"}
            student = _resolve_oidc_user(db, {
                "display_name": "Google", "auto_create_users": True,
                "default_role": "viewer", "default_group_id": 1,
            }, {"iss": "https://accounts.google.com", "sub": "test-student",
                "email": "student@example.test", "email_verified": True}, {})
            db.commit()
            assert student["role"] == "viewer"
            assert student["group_id"] == 1
            assert db.execute(text("SELECT COUNT(*) FROM users")).scalar() == 3
    finally:
        engine.dispose()


@pytest.mark.parametrize("tier", ["community", "pro", "education", "enterprise"])
@pytest.mark.parametrize("caps", [{}, {"max_printers": None, "max_users": None},
                                  *[{"max_printers": cap, "max_users": cap} for cap in [0, 2, 374, 9999]]])
def test_signed_printer_and_user_caps_and_public_json(signed_license, monkeypatch, tier, caps):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from modules.system import routes_health

    lm, info = signed_license(tier=tier, **caps)
    assert info.valid
    for field, check in [("max_printers", lm.check_printer_limit), ("max_users", lm.check_user_limit)]:
        expected = caps.get(field)
        if expected is None:
            expected = lm.TIERS[tier][field]
        assert getattr(info, field) == expected
        if expected is None:
            check(10000)
        else:
            if expected > 0:
                check(expected - 1)
            with pytest.raises(HTTPException) as exc:
                check(expected)
            assert exc.value.status_code == 403
    monkeypatch.setattr(routes_health, "get_license", lambda: info)
    app = FastAPI()
    app.add_api_route("/license", routes_health.get_license_info)
    response = TestClient(app).get("/license")
    assert response.status_code == 200
    # Strict JSON serialization ensures unlimited remains null, never Infinity.
    data = json.loads(response.text, parse_constant=lambda value: pytest.fail(value))
    assert data["max_printers"] == info.max_printers
    assert data["max_users"] == info.max_users


@pytest.mark.parametrize("failure", ["expired", "binding", "signature"])
def test_failed_pro_entitlements_fall_back_to_community(signed_license, monkeypatch, failure):
    import license_manager as lm
    overrides = {"tier": "pro", "max_printers": None, "max_users": None}
    if failure == "expired":
        overrides["expires_at"] = "2000-01-01"
    if failure == "binding":
        overrides["installation_id"] = "synthetic-other-installation"
    lm, info = signed_license(**overrides)
    if failure == "signature":
        path = lm._find_license_file()
        payload, signature = Path(path).read_text().split(".")
        Path(path).write_text(payload + "." + base64.urlsafe_b64encode(b"invalid").decode())
        info = lm.load_license()
        monkeypatch.setattr(lm, "get_license", lambda: info)
    assert not info.valid
    assert info.max_printers == 5
    assert info.max_users == 1
    with pytest.raises(HTTPException):
        lm.check_printer_limit(5)
    with pytest.raises(HTTPException):
        lm.check_user_limit(1)


@pytest.mark.parametrize("cap,status", [(None, 201), (9999, 403), (0, 403)])
@pytest.mark.parametrize("resource", ["printers", "users"])
def test_real_creation_http_at_10000_uses_only_count_adapter(signed_license, tmp_path, monkeypatch, cap, status, resource):
    """Real route insert and DB; adapt only the count to avoid 10,000 fixture rows."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Query
    from sqlalchemy.pool import StaticPool
    from core.auth import UserCreate
    from core.schema import bootstrap_database
    from modules.printers.schemas import PrinterCreate, PrinterResponse
    from modules.printers import routes_crud
    from modules.printers.models import Printer
    from modules.organizations import routes_users

    lm, _ = signed_license(tier="pro", max_printers=cap, max_users=cap)
    monkeypatch.setattr(routes_users, "check_user_limit", lm.check_user_limit)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    bootstrap_database(engine, Path(__file__).resolve().parents[2] / "backend")
    app = FastAPI()
    with Session(engine) as db:
        if resource == "printers":
            original_count = Query.count
            def count(query):
                if query.column_descriptions[0].get("entity") is Printer:
                    return 10000
                return original_count(query)
            monkeypatch.setattr(Query, "count", count)
            def endpoint(printer: PrinterCreate):
                return routes_crud.create_printer(printer, current_user={"id": 1, "role": "admin", "group_id": None}, db=db)
            app.add_api_route("/printers", endpoint, methods=["POST"], response_model=PrinterResponse, status_code=201)
            payload = {"name": "Synthetic cap printer", "model": "Synthetic", "api_type": "bambu", "slot_count": 1}
        else:
            original_execute = db.execute
            class CountResult:
                def scalar(self):
                    return 10000
            def execute(statement, *args, **kwargs):
                if str(statement).strip() == "SELECT COUNT(*) FROM users":
                    return CountResult()
                return original_execute(statement, *args, **kwargs)
            monkeypatch.setattr(db, "execute", execute)
            async def endpoint(user: UserCreate):
                return await routes_users.create_user(user, current_user={"id": 1, "role": "admin", "group_id": None}, db=db)
            app.add_api_route("/users", endpoint, methods=["POST"], status_code=201)
            payload = {"username": "synthetic-cap-user", "email": "synthetic@example.test", "password": "SyntheticPassword123!", "role": "viewer"}
        response = TestClient(app).post(f"/{resource}", json=payload)
        assert response.status_code == status, response.text
        actual_count = db.connection().execute(text(f"SELECT COUNT(*) FROM {resource}")).scalar()
        assert actual_count == (1 if status == 201 else 0)
    engine.dispose()
