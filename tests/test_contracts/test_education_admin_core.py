from __future__ import annotations

import sys
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.errors import OdinError


class _EducationLicense:
    valid = True
    tier = "education"

    @staticmethod
    def has_feature(feature: str) -> bool:
        return feature == "education_workflows"


@pytest.fixture()
def education_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from core.schema import bootstrap_database
    from modules.organizations import education_access

    engine = create_engine(f"sqlite:///{tmp_path / 'education-admin.db'}")
    bootstrap_database(engine, BACKEND)
    monkeypatch.setattr(education_access, "get_license", lambda: _EducationLicense())
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO groups (id, name, is_org) VALUES (1, 'school', 1), (2, 'other', 1)")
        )
        connection.execute(
            text(
                "INSERT INTO users (id, username, email, password_hash, role, is_active, group_id) VALUES "
                "(1, 'tenant-admin', 'admin@example.test', 'fixture', 'admin', 1, 1), "
                "(2, 'student', 'student@example.test', 'fixture', 'viewer', 1, 1), "
                "(3, 'manager', 'manager@example.test', 'fixture', 'viewer', 1, 1), "
                "(4, 'outsider', 'outside@example.test', 'fixture', 'viewer', 1, 2)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO printers (id, name, is_active, shared, org_id, api_type, machine_type) "
                "VALUES (1, 'P1S-01', 1, 0, 1, 'bambu', 'P1S'), "
                "(2, 'outside-printer', 1, 0, 2, 'bambu', 'X1C')"
            )
        )
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _principal(user_id: int = 1, role: str = "admin", group_id: int = 1) -> dict:
    return {
        "id": user_id,
        "username": "fixture",
        "role": role,
        "group_id": group_id,
        "is_active": True,
        "_auth_kind": "session_jwt",
    }


def _run(coroutine):
    return asyncio.run(coroutine)


def _education_client(education_db: Session, principal: dict | None = None) -> TestClient:
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.organizations.routes_education import router

    app = FastAPI()

    @app.exception_handler(OdinError)
    async def _odin_error_handler(request: Request, exc: OdinError):
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        errors = jsonable_encoder(exc.errors())
        fields = sorted(
            {
                ".".join(str(part) for part in item.get("loc", ()) if part != "body")
                for item in errors
                if item.get("loc")
            }
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": errors,
                "error": {
                    "code": "validation_failed",
                    "detail": "Request validation failed",
                    "retriable": False,
                    "fields": fields,
                },
            },
        )

    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: principal or _principal()
    app.dependency_overrides[get_db] = lambda: education_db
    return TestClient(app)


def test_cost_center_replay_grants_printers_and_reload_contract(
    education_db: Session,
) -> None:
    from modules.organizations.education_schemas import (
        CostCenterCreate,
        GrantInput,
        GrantReplacement,
        PrinterReplacement,
    )
    from modules.organizations.routes_education import (
        create_cost_center,
        list_cost_center_grants,
        list_cost_center_printers,
        replace_cost_center_grants,
        replace_cost_center_printers,
    )

    command_id = uuid4()
    create_body = CostCenterCreate(
        name="Robotics Club",
        code="ROB",
        description="Fixture",
        command_id=command_id,
    )
    created = _run(create_cost_center(create_body, _principal(), education_db))
    replayed = _run(create_cost_center(create_body, _principal(), education_db))
    assert replayed == created
    assert education_db.execute(
        text("SELECT COUNT(*) FROM education_cost_centers")
    ).scalar_one() == 1

    grants = _run(replace_cost_center_grants(
        created["id"],
        GrantReplacement(
            revision=1,
            grants=[
                GrantInput(user_id=2, roles=["student"]),
                GrantInput(user_id=3, roles=["manager"]),
            ],
            command_id=uuid4(),
        ),
        _principal(),
        education_db,
    ))
    assert grants == {
        "id": created["id"],
        "revision": 2,
        "grants": [
            {"user_id": 2, "roles": ["student"]},
            {"user_id": 3, "roles": ["manager"]},
        ],
    }

    first_page = _run(list_cost_center_grants(
        created["id"], "active", 1, None, None, _principal(), education_db
    ))
    assert len(first_page["items"]) == 1
    assert first_page["next_cursor"]
    assert "email" not in first_page["items"][0]
    second_page = _run(list_cost_center_grants(
        created["id"],
        "active",
        1,
        first_page["next_cursor"],
        None,
        _principal(),
        education_db,
    ))
    assert len(second_page["items"]) == 1
    assert second_page["next_cursor"] is None

    printers = _run(replace_cost_center_printers(
        created["id"],
        PrinterReplacement(
            revision=2,
            printer_ids=[1],
            command_id=uuid4(),
        ),
        _principal(),
        education_db,
    ))
    assert printers == {"id": created["id"], "revision": 3, "printer_ids": [1]}
    printer_page = _run(list_cost_center_printers(
        created["id"], "active", 50, None, None, _principal(), education_db
    ))
    assert printer_page["center_revision"] == 3
    assert printer_page["items"][0] == {
        "entitlement_id": printer_page["items"][0]["entitlement_id"],
        "printer_id": 1,
        "name": "P1S-01",
        "machine_type": "P1S",
        "api_type": "bambu",
        "state": "active",
        "granted_at": printer_page["items"][0]["granted_at"],
        "revoked_at": None,
    }
    assert "api_key" not in printer_page["items"][0]
    assert "api_host" not in printer_page["items"][0]


def test_replacement_rejects_stale_revision_and_cross_tenant_members(
    education_db: Session,
) -> None:
    from modules.organizations.education_schemas import (
        CostCenterCreate,
        GrantInput,
        GrantReplacement,
    )
    from modules.organizations.routes_education import (
        create_cost_center,
        replace_cost_center_grants,
    )

    created = _run(create_cost_center(
        CostCenterCreate(name="Class A", code="A", command_id=uuid4()),
        _principal(),
        education_db,
    ))
    with pytest.raises(OdinError, match="cross-tenant"):
        _run(replace_cost_center_grants(
            created["id"],
            GrantReplacement(
                revision=1,
                grants=[GrantInput(user_id=4, roles=["student"])],
                command_id=uuid4(),
            ),
            _principal(),
            education_db,
        ))
    with pytest.raises(OdinError) as exc:
        _run(replace_cost_center_grants(
            created["id"],
            GrantReplacement(revision=999, grants=[], command_id=uuid4()),
            _principal(),
            education_db,
        ))
    assert exc.value.status == 409
    assert exc.value.detail == "reload_required"


def test_capabilities_are_server_derived_and_auth_provenance_fails_closed(
    education_db: Session,
) -> None:
    from modules.organizations.education_access import (
        _education_token_allowed,
        capabilities_for,
    )

    education_db.execute(
        text(
            "INSERT INTO education_cost_centers "
            "(id, org_id, name_key, code_key, display_name, code, created_by) "
            "VALUES (1, 1, 'class', 'c', 'Class', 'C', 1)"
        )
    )
    education_db.execute(
        text(
            "INSERT INTO education_cost_center_grants "
            "(org_id, cost_center_id, user_id, role, granted_by) VALUES "
            "(1, 1, 2, 'student', 1), (1, 1, 3, 'manager', 1)"
        )
    )
    education_db.commit()

    student = capabilities_for(education_db, _principal(2, "viewer", 1))
    manager = capabilities_for(education_db, _principal(3, "viewer", 1))
    admin = capabilities_for(education_db, _principal())
    unassigned = capabilities_for(education_db, _principal(1, "admin", None))
    assert student["student_cost_center_ids"] == [1]
    assert student["manager"] is False
    assert manager["managed_cost_center_ids"] == [1]
    assert admin["tenant_admin"] is True
    assert unassigned == {
        "education_enabled": False,
        "student": False,
        "manager": False,
        "tenant_admin": False,
        "student_cost_center_ids": [],
        "managed_cost_center_ids": [],
    }

    assert not _education_token_allowed(
        {"_auth_kind": "legacy_global_api_key"}, write=False
    )
    assert not _education_token_allowed(
        {"_auth_kind": "user_api_token", "_token_scopes": []}, write=False
    )
    assert _education_token_allowed(
        {"_auth_kind": "user_api_token", "_token_scopes": ["read:education"]},
        write=False,
    )
    assert not _education_token_allowed(
        {"_auth_kind": "user_api_token", "_token_scopes": ["read:education"]},
        write=True,
    )


def test_policy_inventory_names_smart_plug_as_physical_action() -> None:
    from modules.organizations.education_policy_inventory import POLICY_INVENTORY

    smart_plug = [
        item for item in POLICY_INVENTORY if item["surface"] == "printers.smart_plug"
    ]
    assert len(smart_plug) == 1
    assert smart_plug[0]["operation"] == "dispatch"
    assert smart_plug[0]["disposition"] == "batch2_required"
    assert smart_plug[0]["focused_test"] == "test_education_dispatch_policy.py"


def test_capabilities_are_http_200_and_all_false_without_entitlement(
    education_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.organizations import education_access
    from modules.organizations.routes_education import router

    class _NoEducationLicense:
        valid = True
        tier = "pro"

        @staticmethod
        def has_feature(feature: str) -> bool:
            return False

    monkeypatch.setattr(education_access, "get_license", lambda: _NoEducationLicense())
    app = FastAPI()

    @app.exception_handler(OdinError)
    async def _odin_error_handler(request: Request, exc: OdinError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _principal(2, "viewer", 1)
    app.dependency_overrides[get_db] = lambda: education_db
    response = TestClient(app).get("/education/capabilities")
    assert response.status_code == 200
    assert response.json() == {
        "education_enabled": False,
        "student": False,
        "manager": False,
        "tenant_admin": False,
        "student_cost_center_ids": [],
        "managed_cost_center_ids": [],
    }


def test_center_list_detail_archive_and_reopen_are_reloadable(
    education_db: Session,
) -> None:
    from modules.organizations.education_schemas import (
        CostCenterCreate,
        CostCenterLifecycle,
    )
    from modules.organizations.routes_education import (
        archive_cost_center,
        create_cost_center,
        get_cost_center,
        list_cost_centers,
        reopen_cost_center,
    )

    alpha = _run(create_cost_center(
        CostCenterCreate(name="Alpha Class", code="A", command_id=uuid4()),
        _principal(), education_db,
    ))
    _run(create_cost_center(
        CostCenterCreate(name="zeta club", code="Z", command_id=uuid4()),
        _principal(), education_db,
    ))
    page_one = _run(list_cost_centers(None, False, 1, None, _principal(), education_db))
    assert page_one["items"][0]["name"] == "Alpha Class"
    assert page_one["next_cursor"]
    page_two = _run(list_cost_centers(
        None, False, 1, page_one["next_cursor"], _principal(), education_db
    ))
    assert page_two["items"][0]["name"] == "zeta club"

    archive_body = CostCenterLifecycle(
        revision=1, reason="Class ended", command_id=uuid4()
    )
    archived = _run(archive_cost_center(
        alpha["id"],
        archive_body,
        _principal(), education_db,
    ))
    assert _run(archive_cost_center(
        alpha["id"], archive_body, _principal(), education_db
    )) == archived
    assert archived["active"] is False
    detail = _run(get_cost_center(alpha["id"], None, _principal(), education_db))
    assert detail == archived
    reopen_body = CostCenterLifecycle(
        revision=2, reason="New semester", command_id=uuid4()
    )
    reopened = _run(reopen_cost_center(
        alpha["id"],
        reopen_body,
        _principal(), education_db,
    ))
    assert _run(reopen_cost_center(
        alpha["id"], reopen_body, _principal(), education_db
    )) == reopened
    assert reopened["active"] is True
    assert reopened["revision"] == 3

    audit_details = [
        row.details_json
        for row in education_db.execute(
            text(
                "SELECT details_json FROM education_audit_events "
                "WHERE resource_id=:id ORDER BY created_at"
            ),
            {"id": str(alpha["id"])},
        ).fetchall()
    ]
    assert any('"reason":"Class ended"' in details for details in audit_details)
    assert any('"reason":"New semester"' in details for details in audit_details)


def test_replacement_history_keeps_canonical_before_and_after_snapshots(
    education_db: Session,
) -> None:
    import json

    from modules.organizations.education_schemas import (
        CostCenterCreate,
        GrantInput,
        GrantReplacement,
    )
    from modules.organizations.routes_education import (
        create_cost_center,
        replace_cost_center_grants,
    )

    center = _run(create_cost_center(
        CostCenterCreate(name="History", code="H", command_id=uuid4()),
        _principal(), education_db,
    ))
    _run(replace_cost_center_grants(
        center["id"],
        GrantReplacement(
            revision=1,
            grants=[GrantInput(user_id=2, roles=["student", "manager"])],
            command_id=uuid4(),
        ),
        _principal(), education_db,
    ))
    _run(replace_cost_center_grants(
        center["id"],
        GrantReplacement(revision=2, grants=[], command_id=uuid4()),
        _principal(), education_db,
    ))
    snapshots = [
        json.loads(row.details_json)
        for row in education_db.execute(
            text(
                "SELECT details_json FROM education_audit_events "
                "WHERE action='cost_center.grants.replace' ORDER BY created_at"
            )
        ).fetchall()
    ]
    assert snapshots[0] == {
        "before": [],
        "after": [{"user_id": 2, "roles": ["manager", "student"]}],
    }
    assert snapshots[1] == {
        "before": [{"user_id": 2, "roles": ["manager", "student"]}],
        "after": [],
    }


def test_concurrent_identical_command_returns_one_stored_result(
    education_db: Session,
) -> None:
    from modules.organizations.education_schemas import (
        CostCenterCreate,
        GrantInput,
        GrantReplacement,
    )
    from modules.organizations.routes_education import (
        create_cost_center,
        replace_cost_center_grants,
    )

    center = _run(create_cost_center(
        CostCenterCreate(name="Race", code="RACE", command_id=uuid4()),
        _principal(), education_db,
    ))
    command_id = uuid4()

    def execute() -> dict:
        session = Session(education_db.get_bind())
        try:
            return _run(replace_cost_center_grants(
                center["id"],
                GrantReplacement(
                    revision=1,
                    grants=[GrantInput(user_id=2, roles=["student"])],
                    command_id=command_id,
                ),
                _principal(),
                session,
            ))
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(), range(2)))
    assert results[0] == results[1]
    assert education_db.execute(
        text(
            "SELECT COUNT(*) FROM education_audit_events "
            "WHERE action='cost_center.grants.replace' AND command_id=:command_id"
        ),
        {"command_id": str(command_id)},
    ).scalar_one() == 1


def test_exact_replacement_replay_precedes_changed_target_validation(
    education_db: Session,
) -> None:
    from modules.organizations.education_schemas import (
        CostCenterCreate,
        GrantInput,
        GrantReplacement,
        PrinterReplacement,
    )
    from modules.organizations.routes_education import (
        create_cost_center,
        replace_cost_center_grants,
        replace_cost_center_printers,
    )

    center = _run(create_cost_center(
        CostCenterCreate(name="Replay", code="REPLAY", command_id=uuid4()),
        _principal(), education_db,
    ))
    grant_body = GrantReplacement(
        revision=1,
        grants=[GrantInput(user_id=2, roles=["student"])],
        command_id=uuid4(),
    )
    first_grants = _run(replace_cost_center_grants(
        center["id"], grant_body, _principal(), education_db
    ))
    education_db.execute(text("UPDATE users SET is_active=0 WHERE id=2"))
    education_db.commit()
    assert _run(replace_cost_center_grants(
        center["id"], grant_body, _principal(), education_db
    )) == first_grants

    printer_body = PrinterReplacement(
        revision=2, printer_ids=[1], command_id=uuid4()
    )
    first_printers = _run(replace_cost_center_printers(
        center["id"], printer_body, _principal(), education_db
    ))
    education_db.execute(text("UPDATE printers SET is_active=0 WHERE id=1"))
    education_db.commit()
    assert _run(replace_cost_center_printers(
        center["id"], printer_body, _principal(), education_db
    )) == first_printers


def test_education_admin_http_workflow_and_validation_contract(
    education_db: Session,
) -> None:
    client = _education_client(education_db)
    created_response = client.post(
        "/education/cost-centers",
        json={"name": "Fabrication", "code": "FAB", "command_id": str(uuid4())},
    )
    assert created_response.status_code == 201
    center = created_response.json()
    center_id = center["id"]
    assert client.get("/education/cost-centers").json()["items"] == [center]
    assert client.get(f"/education/cost-centers/{center_id}").json() == center

    grants = client.put(
        f"/education/cost-centers/{center_id}/grants",
        json={
            "revision": 1,
            "grants": [{"user_id": 2, "roles": ["student"]}],
            "command_id": str(uuid4()),
        },
    )
    assert grants.status_code == 200
    assert grants.json()["revision"] == 2
    assert client.get(
        f"/education/cost-centers/{center_id}/grants"
    ).json()["items"][0]["user_id"] == 2

    printers = client.put(
        f"/education/cost-centers/{center_id}/printers",
        json={"revision": 2, "printer_ids": [1], "command_id": str(uuid4())},
    )
    assert printers.status_code == 200
    assert printers.json() == {"id": center_id, "revision": 3, "printer_ids": [1]}
    assert client.get(
        f"/education/cost-centers/{center_id}/printers"
    ).json()["items"][0]["printer_id"] == 1

    archived = client.post(
        f"/education/cost-centers/{center_id}/archive",
        json={"revision": 3, "reason": "Term complete", "command_id": str(uuid4())},
    )
    assert archived.status_code == 200
    assert archived.json()["active"] is False
    reopened = client.post(
        f"/education/cost-centers/{center_id}/reopen",
        json={"revision": 4, "reason": "New term", "command_id": str(uuid4())},
    )
    assert reopened.status_code == 200
    assert reopened.json()["active"] is True

    invalid = client.post(
        f"/education/cost-centers/{center_id}/archive",
        json={"revision": 5, "reason": "   ", "command_id": str(uuid4())},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_failed"
    assert invalid.json()["error"]["fields"] == ["reason"]


def test_user_lifecycle_guard_includes_printer_entitlement_actors(
    education_db: Session,
) -> None:
    from fastapi import HTTPException
    from modules.organizations.education_policy import (
        assert_user_hard_delete_allowed,
        assert_user_tenant_change_allowed,
    )

    education_db.execute(
        text(
            "INSERT INTO education_cost_centers "
            "(id, org_id, name_key, code_key, display_name, code, created_by) "
            "VALUES (10, 1, 'guard', 'guard', 'Guard', 'GUARD', 1)"
        )
    )
    education_db.execute(
        text(
            "INSERT INTO education_cost_center_printers "
            "(org_id, cost_center_id, printer_id, granted_by) VALUES (1, 10, 1, 3)"
        )
    )
    education_db.commit()
    with pytest.raises(HTTPException) as tenant_change:
        assert_user_tenant_change_allowed(education_db, 3)
    assert tenant_change.value.status_code == 409
    with pytest.raises(HTTPException) as hard_delete:
        assert_user_hard_delete_allowed(education_db, 3)
    assert hard_delete.value.status_code == 409


@pytest.mark.parametrize("reference", ["outbox", "rate", "storage"])
def test_user_tenant_change_guard_covers_composite_user_references(
    education_db: Session, reference: str,
) -> None:
    from fastapi import HTTPException
    from modules.organizations.education_policy import assert_user_tenant_change_allowed

    if reference == "outbox":
        education_db.execute(
            text(
                "INSERT INTO education_audit_events "
                "(event_id, org_id, actor_kind, actor_id, action, command_id, "
                "request_hash, resource_type, resource_id) VALUES "
                "('guard-event', 1, 'system', 'system', 'guard', 'guard-command', "
                "'hash', 'user', '2')"
            )
        )
        education_db.execute(
            text(
                "INSERT INTO education_notification_outbox "
                "(event_id, org_id, recipient_user_id) VALUES ('guard-event', 1, 2)"
            )
        )
    elif reference == "rate":
        education_db.execute(
            text(
                "INSERT INTO education_rate_counters "
                "(org_id, scope_kind, scope_id, user_id, bucket_kind, bucket_start) "
                "VALUES (1, 'user', 2, 2, 'hour', CURRENT_TIMESTAMP)"
            )
        )
    else:
        education_db.execute(
            text(
                "INSERT INTO education_storage_accounts "
                "(org_id, scope_kind, scope_id, user_id) VALUES (1, 'user', 2, 2)"
            )
        )
    education_db.commit()

    with pytest.raises(HTTPException) as blocked:
        assert_user_tenant_change_allowed(education_db, 2)
    assert blocked.value.status_code == 409


def test_ws_token_contains_live_capability_snapshot(
    education_db: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jwt
    from core import auth as auth_module
    from core import db as core_db
    from core.app import ConnectionManager
    from modules.organizations.education_access import capability_snapshot_id
    from modules.organizations.routes_auth import get_ws_token
    from sqlalchemy.orm import sessionmaker

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/ws-token",
            "headers": [],
            "client": ("test", 123),
            "scheme": "http",
            "server": ("test", 80),
        }
    )
    principal = _principal()
    token = _run(get_ws_token(request, principal, education_db))["token"]
    payload = jwt.decode(
        token, auth_module.SECRET_KEY, algorithms=[auth_module.ALGORITHM]
    )
    assert payload["ws"] is True
    assert payload["jti"]
    assert payload["capability_snapshot_id"] == capability_snapshot_id(
        education_db, principal
    )
    ws_principal = {
        **principal,
        "_auth_kind": "websocket_token",
        "_capability_snapshot_id": payload["capability_snapshot_id"],
    }
    monkeypatch.setattr(
        core_db, "SessionLocal", sessionmaker(bind=education_db.get_bind())
    )
    assert ConnectionManager._education_principal_current(
        ws_principal, {"education_internal": True, "data": {}}
    )
    education_db.execute(text("UPDATE users SET role='viewer' WHERE id=1"))
    education_db.commit()
    assert not ConnectionManager._education_principal_current(
        ws_principal, {"education_internal": True, "data": {}}
    )
