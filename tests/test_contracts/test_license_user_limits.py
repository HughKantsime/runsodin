"""Signed nullable entitlements must produce usable, tier-aware user limits."""

import asyncio
import base64
import json

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
    assert response.json()["max_users"] == 9999
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
