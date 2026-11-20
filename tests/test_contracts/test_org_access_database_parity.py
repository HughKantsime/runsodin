"""Exercise the member picker and its write guards on both supported databases."""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from core.db import get_db
from core.dependencies import get_current_user
from core.schema import bootstrap_database
from modules.organizations import routes, routes_users
from modules.organizations.education_policy import assert_user_tenant_change_allowed
from modules.organizations.routes_users import write_group_fields
from tests.test_contracts.test_education_classes_migration import pg_engine  # noqa: F401


@pytest.fixture(scope="module", params=["sqlite", pytest.param("postgresql", marks=pytest.mark.postgresql)])
def access_engine(request):
    if request.param == "postgresql":
        engine = request.getfixturevalue("pg_engine")
    else:
        engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        request.addfinalizer(engine.dispose)
    bootstrap_database(engine)
    return engine


@pytest.fixture
def db(access_engine):
    with Session(access_engine) as session:
        session.execute(text("INSERT INTO groups (id,name,is_org) VALUES (1,'school',TRUE),(2,'other',TRUE)"))
        session.execute(text(
            "INSERT INTO users (id,username,role,password_hash,is_active,group_id) VALUES "
            "(1,'platform','admin','fixture',TRUE,NULL),(2,'admin','admin','fixture',TRUE,1),"
            "(3,'member','operator','fixture',TRUE,1),(4,'outsider','operator','fixture',TRUE,2),"
            "(5,'available','viewer','fixture',TRUE,NULL),(6,'recorded','viewer','fixture',TRUE,NULL)"
        ))
        session.execute(text(
            "INSERT INTO education_audit_events "
            "(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,resource_id) "
            "VALUES (:actor,1,'user',:actor,'review','command','hash','user',:actor)"
        ), [{"actor": "6"}, {"actor": "7"}])  # Actor 7 has history but no users row.
        yield session


@pytest.mark.parametrize(("admin_id", "expected"), [
    pytest.param(1, [(5, "available"), (4, "outsider")], id="platform-admin"),
    pytest.param(2, [(5, "available")], id="org-admin"),
])
def test_member_candidates(db, admin_id, expected, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    admin = db.execute(text("SELECT id,role,group_id FROM users WHERE id=:id"), {"id": admin_id}).mappings().one()
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(routes, "require_feature", lambda feature: None)
    with TestClient(app) as client:
        response = client.get("/orgs/1/member-candidates")
    assert response.status_code == 200
    assert response.json() == [{"id": uid, "username": name} for uid, name in expected]


def test_tenant_change_checks_history_even_without_a_user_row(db):
    assert_user_tenant_change_allowed(db, 5)
    for user_id in (6, 7):
        with pytest.raises(HTTPException) as refused:
            assert_user_tenant_change_allowed(db, user_id)
        assert refused.value.status_code == 409


@pytest.mark.parametrize("path,caller_id", [
    (path, caller_id) for path in ("/groups", "/groups/1", "/orgs")
    for caller_id in ((1, 2) if path == "/orgs" else (1, 2, 3))
])
def test_owner_visibility_follows_membership(db, monkeypatch, path, caller_id):
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(routes_users.router)
    caller = db.execute(text("SELECT id,role,group_id FROM users WHERE id=:id"), {"id": caller_id}).mappings().one()
    app.dependency_overrides[get_current_user] = lambda: caller
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(routes, "require_feature", lambda feature: None)
    monkeypatch.setattr(routes_users, "require_feature", lambda feature: None)
    with TestClient(app) as client:
        for owner_id, username in [(3, "member"), (1, "platform"), (4, "outsider")]:
            db.execute(text("UPDATE groups SET owner_id=:owner WHERE id=1"), {"owner": owner_id})
            response = client.get(path)
            assert response.status_code == 200
            data = response.json()
            group = data if path == "/groups/1" else next(g for g in data if g["id"] == 1)
            visible = caller_id == 1 or owner_id == 3
            assert group["owner_id"] == (owner_id if visible else None)
            if path != "/orgs":
                assert group["owner_username"] == (username if visible else None)


def test_scoped_owner_update_checks_matched_rows(db):
    admin = {"id": 2, "role": "admin", "group_id": 1}
    owner = text("SELECT owner_id FROM groups WHERE id=1")
    write_group_fields(db, 1, {"owner_id": 3}, admin)
    assert db.execute(owner).scalar_one() == 3
    write_group_fields(db, 1, {"owner_id": 3}, admin)  # An unchanged owner still matches one row.
    with pytest.raises(HTTPException) as refused:
        write_group_fields(db, 1, {"owner_id": 4}, admin)
    assert refused.value.status_code == 400
    assert db.execute(owner).scalar_one() == 3
