from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def classroom_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from core.schema import bootstrap_database

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    engine = create_engine(f"sqlite:///{tmp_path / 'classroom.db'}")
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO groups (id, name, is_org) VALUES (1, 'CTEC', 1), (2, 'Other', 1)"))
        connection.execute(
            text(
                "INSERT INTO users (id, username, email, password_hash, role, is_active, group_id) "
                "VALUES (1, 'admin', 'admin@ctechigh.org', 'fixture', 'admin', 1, 1)"
            )
        )
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _configure(db: Session):
    from modules.organizations.classroom_service import configure_connection

    return configure_connection(
        db,
        org_id=1,
        admin_id=1,
        client_id="google-client-id.apps.googleusercontent.com",
        client_secret="top-secret-client-value",
        allowed_domains="ctechigh.org",
    )


def _roster(*, include_student: bool = True) -> dict:
    return {
        "course": {
            "id": "course-123",
            "name": "Engineering Design",
            "section": "ENG-101",
            "description": "Fall pilot",
            "course_state": "ACTIVE",
        },
        "teachers": [
            {"provider_user_id": "google-teacher", "email": "teacher@ctechigh.org", "name": "Teacher One"}
        ],
        "students": [
            {"provider_user_id": "google-student", "email": "student@ctechigh.org", "name": "Student One"}
        ] if include_student else [],
    }


def test_connect_url_uses_pkce_exact_readonly_scopes_and_encrypted_state(classroom_db: Session) -> None:
    from modules.organizations.classroom_service import CLASSROOM_SCOPES, create_connect_url

    status = _configure(classroom_db)
    assert status["configured"] is True
    row = classroom_db.execute(text("SELECT * FROM classroom_connections WHERE org_id=1")).one()
    assert row.client_secret_encrypted != "top-secret-client-value"
    assert "top-secret-client-value" not in str(status)

    url = create_connect_url(
        classroom_db,
        org_id=1,
        admin_id=1,
        redirect_uri="https://odin.example/api/education/classroom/callback",
    )
    query = parse_qs(urlparse(url).query)
    assert query["scope"][0].split() == list(CLASSROOM_SCOPES)
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["hd"] == ["ctechigh.org"]
    pending = classroom_db.execute(
        text("SELECT * FROM classroom_oauth_states WHERE state=:state"), {"state": query["state"][0]}
    ).one()
    assert pending.code_verifier_encrypted not in url
    assert "top-secret-client-value" not in url


def test_callback_connects_once_and_never_returns_tokens(
    classroom_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from modules.organizations import classroom_service
    from modules.organizations.classroom_service import (
        CLASSROOM_SCOPES,
        ClassroomError,
        complete_connection,
        create_connect_url,
    )

    _configure(classroom_db)
    url = create_connect_url(
        classroom_db,
        org_id=1,
        admin_id=1,
        redirect_uri="https://odin.example/api/education/classroom/callback",
    )
    state = parse_qs(urlparse(url).query)["state"][0]

    async def fake_request(method, url, *, headers=None, data=None):
        if "token" in url:
            return {
                "access_token": "access-token-secret",
                "refresh_token": "refresh-token-secret",
                "expires_in": 3600,
                "scope": " ".join(CLASSROOM_SCOPES),
            }
        return {
            "sub": "google-admin",
            "email": "admin@ctechigh.org",
            "email_verified": True,
        }

    monkeypatch.setattr(classroom_service, "_json_request", fake_request)
    result = asyncio.run(
        complete_connection(
            classroom_db,
            state=state,
            code="authorization-code",
            principal={"id": 1, "group_id": 1},
        )
    )
    assert result["connected"] is True
    assert "token" not in str(result).lower()
    stored = classroom_db.execute(text("SELECT * FROM classroom_connections WHERE org_id=1")).one()
    assert stored.refresh_token_encrypted != "refresh-token-secret"
    assert stored.access_token_encrypted != "access-token-secret"
    with pytest.raises(ClassroomError, match="invalid or expired"):
        asyncio.run(
            complete_connection(
                classroom_db,
                state=state,
                code="authorization-code",
                principal={"id": 1, "group_id": 1},
            )
        )


def test_callback_missing_scopes_consumes_state_without_connection(
    classroom_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from modules.organizations import classroom_service
    from modules.organizations.classroom_service import ClassroomError, complete_connection, create_connect_url

    _configure(classroom_db)
    url = create_connect_url(classroom_db, org_id=1, admin_id=1, redirect_uri="https://odin.example/callback")
    state = parse_qs(urlparse(url).query)["state"][0]

    async def fake_request(method, url, *, headers=None, data=None):
        return {"access_token": "access", "refresh_token": "refresh", "scope": "openid email"}

    monkeypatch.setattr(classroom_service, "_json_request", fake_request)
    with pytest.raises(ClassroomError) as exc:
        asyncio.run(complete_connection(classroom_db, state=state, code="code", principal={"id": 1, "group_id": 1}))
    assert exc.value.code == "classroom_scopes_missing"
    assert classroom_db.execute(text("SELECT COUNT(*) FROM classroom_oauth_states WHERE state=:state"), {"state": state}).scalar_one() == 0
    assert classroom_db.execute(text("SELECT state FROM classroom_connections WHERE org_id=1")).scalar_one() == "not_connected"


def test_atomic_import_creates_viewers_grants_mapping_and_idempotent_resync(
    classroom_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from modules.organizations import routes_classroom
    from modules.organizations.routes_classroom import ClassroomImportInput, import_classroom_course

    _configure(classroom_db)

    async def full_roster(db, org_id, course_id):
        return _roster(include_student=True)

    monkeypatch.setattr(routes_classroom, "get_roster", full_roster)
    command_id = uuid4()
    body = ClassroomImportInput(command_id=command_id)
    principal = {"id": 1, "role": "admin", "group_id": 1, "is_active": True}
    first = asyncio.run(import_classroom_course("course-123", body, principal, classroom_db))
    assert first["created_cost_center"] is True
    assert first["teachers"] == 1 and first["students"] == 1
    users = classroom_db.execute(
        text("SELECT email, role, password_hash, is_active, group_id FROM users WHERE id<>1 ORDER BY email")
    ).fetchall()
    assert [tuple(row) for row in users] == [
        ("student@ctechigh.org", "viewer", "", True, 1),
        ("teacher@ctechigh.org", "viewer", "", True, 1),
    ]
    grants = classroom_db.execute(
        text("SELECT u.email, g.role, g.state FROM education_cost_center_grants g JOIN users u ON u.id=g.user_id ORDER BY u.email")
    ).fetchall()
    assert [tuple(row) for row in grants] == [
        ("student@ctechigh.org", "student", "active"),
        ("teacher@ctechigh.org", "manager", "active"),
    ]
    first_audit = classroom_db.execute(
        text(
            "SELECT details_json FROM education_audit_events "
            "WHERE command_id=:command_id"
        ),
        {"command_id": str(command_id)},
    ).scalar_one()
    first_details = json.loads(first_audit)
    assert first_details["added_grant_count"] == 2
    assert first_details["revoked_grant_count"] == 0
    assert first_details["mapping_created"] is True
    replay = asyncio.run(import_classroom_course("course-123", body, principal, classroom_db))
    assert replay == first
    assert classroom_db.execute(text("SELECT COUNT(*) FROM classroom_course_mappings")).scalar_one() == 1

    async def reduced_roster(db, org_id, course_id):
        return _roster(include_student=False)

    monkeypatch.setattr(routes_classroom, "get_roster", reduced_roster)
    second = asyncio.run(
        import_classroom_course(
            "course-123",
            ClassroomImportInput(command_id=uuid4()),
            principal,
            classroom_db,
        )
    )
    assert second["students"] == 0
    assert classroom_db.execute(
        text("SELECT state FROM education_cost_center_grants g JOIN users u ON u.id=g.user_id WHERE u.email='student@ctechigh.org'")
    ).scalar_one() == "revoked"
    assert classroom_db.execute(text("SELECT COUNT(*) FROM users WHERE email='student@ctechigh.org'")).scalar_one() == 1


def test_cross_tenant_roster_conflict_rolls_back_everything(
    classroom_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.errors import OdinError
    from modules.organizations import routes_classroom
    from modules.organizations.routes_classroom import ClassroomImportInput, import_classroom_course

    _configure(classroom_db)
    classroom_db.execute(
        text("INSERT INTO users (username, email, password_hash, role, is_active, group_id) VALUES ('student-other', 'student@ctechigh.org', '', 'viewer', 1, 2)")
    )
    classroom_db.commit()

    async def roster(db, org_id, course_id):
        return _roster(include_student=True)

    monkeypatch.setattr(routes_classroom, "get_roster", roster)
    with pytest.raises(OdinError) as exc:
        asyncio.run(
            import_classroom_course(
                "course-123",
                ClassroomImportInput(command_id=uuid4()),
                {"id": 1, "role": "admin", "group_id": 1, "is_active": True},
                classroom_db,
            )
        )
    assert exc.value.status == 409
    assert classroom_db.execute(text("SELECT COUNT(*) FROM classroom_course_mappings")).scalar_one() == 0
    assert classroom_db.execute(text("SELECT COUNT(*) FROM education_cost_centers")).scalar_one() == 0
    assert classroom_db.execute(text("SELECT COUNT(*) FROM education_commands")).scalar_one() == 0
    failure = classroom_db.execute(
        text("SELECT action, details FROM audit_logs WHERE action='classroom_import_failed'")
    ).one()
    assert failure.action == "classroom_import_failed"
    assert json.loads(failure.details) == {
        "org_id": 1,
        "actor_id": 1,
        "reason": "classroom_identity_conflict",
    }


def test_preview_failure_is_sanitized_and_audited(
    classroom_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.errors import OdinError
    from modules.organizations import routes_classroom
    from modules.organizations.classroom_service import ClassroomError
    from modules.organizations.routes_classroom import classroom_course_preview

    _configure(classroom_db)

    async def failed_roster(db, org_id, course_id):
        raise ClassroomError(
            "classroom_refresh_failed",
            "provider response containing a sensitive diagnostic",
            409,
        )

    monkeypatch.setattr(routes_classroom, "get_roster", failed_roster)
    with pytest.raises(OdinError):
        asyncio.run(
            classroom_course_preview(
                "course-secret-id",
                None,
                {"id": 1, "role": "admin", "group_id": 1, "is_active": True},
                classroom_db,
            )
        )
    audit = classroom_db.execute(
        text("SELECT details FROM audit_logs WHERE action='classroom_preview_failed'")
    ).scalar_one()
    audit_details = json.loads(audit)
    assert audit_details == {
        "org_id": 1,
        "actor_id": 1,
        "reason": "classroom_refresh_failed",
    }
    assert "sensitive diagnostic" not in str(audit_details)
    assert "course-secret-id" not in str(audit_details)


def test_disconnect_clears_authorization_but_preserves_imported_records(classroom_db: Session) -> None:
    from core.crypto import encrypt
    from modules.organizations.classroom_service import disconnect

    _configure(classroom_db)
    classroom_db.execute(
        text("UPDATE classroom_connections SET state='connected', account_email='admin@ctechigh.org', refresh_token_encrypted=:refresh, access_token_encrypted=:access WHERE org_id=1"),
        {"refresh": encrypt("refresh-secret"), "access": encrypt("access-secret")},
    )
    classroom_db.execute(
        text("INSERT INTO education_cost_centers (id, org_id, name_key, code_key, display_name, code, created_by) VALUES (1, 1, 'class', 'class', 'Class', 'CLASS', 1)")
    )
    classroom_db.execute(
        text("INSERT INTO classroom_course_mappings (org_id, provider_course_id, cost_center_id, course_name) VALUES (1, 'course-1', 1, 'Class')")
    )
    classroom_db.commit()
    result = disconnect(classroom_db, 1)
    assert result["connected"] is False
    stored = classroom_db.execute(text("SELECT * FROM classroom_connections WHERE org_id=1")).one()
    assert stored.refresh_token_encrypted is None and stored.access_token_encrypted is None
    assert classroom_db.execute(text("SELECT COUNT(*) FROM classroom_course_mappings")).scalar_one() == 1


def test_privacy_export_erasure_and_backup_cover_classroom_state(classroom_db: Session) -> None:
    import sqlite3

    from core.crypto import encrypt
    from modules.organizations.routes_sessions import erase_user_data, export_user_data
    from modules.system.backup_service import create_online_backup

    _configure(classroom_db)
    classroom_db.execute(
        text(
            "INSERT INTO users (id, username, email, password_hash, role, is_active, group_id) "
            "VALUES (2, 'student', 'student@ctechigh.org', '', 'viewer', 1, 1)"
        )
    )
    classroom_db.execute(
        text("INSERT INTO education_cost_centers (id, org_id, name_key, code_key, display_name, code, created_by) VALUES (1, 1, 'class', 'class', 'Class', 'CLASS', 1)")
    )
    classroom_db.execute(
        text("INSERT INTO classroom_course_mappings (org_id, provider_course_id, cost_center_id, course_name) VALUES (1, 'course-1', 1, 'Class')")
    )
    classroom_db.execute(
        text("INSERT INTO education_cost_center_grants (org_id, cost_center_id, user_id, role, state, granted_by) VALUES (1, 1, 2, 'student', 'active', 1)")
    )
    classroom_db.execute(
        text("INSERT INTO classroom_roster_identities (org_id, user_id, provider_user_id, normalized_email, state, last_seen_at) VALUES (1, 2, 'google-student', 'student@ctechigh.org', 'active', :now)"),
        {"now": datetime.now(timezone.utc).isoformat()},
    )
    classroom_db.execute(
        text("UPDATE classroom_connections SET account_subject='google-student', account_email='student@ctechigh.org', state='connected', refresh_token_encrypted=:refresh, access_token_encrypted=:access WHERE org_id=1"),
        {"refresh": encrypt("privacy-refresh-secret"), "access": encrypt("privacy-access-secret")},
    )
    classroom_db.commit()

    exported = asyncio.run(
        export_user_data(2, {"id": 1, "role": "admin", "group_id": 1}, classroom_db)
    )
    assert exported["classroom_identity"][0]["provider_user_id"] == "google-student"
    assert exported["classroom_memberships"][0]["provider_course_id"] == "course-1"
    assert "privacy-refresh-secret" not in str(exported)
    assert "privacy-access-secret" not in str(exported)

    database_path = Path(str(classroom_db.get_bind().url.database))
    backup_path, _ = create_online_backup(f"sqlite:///{database_path}")
    with sqlite3.connect(backup_path) as backup:
        tables = {row[0] for row in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "classroom_connections",
            "classroom_oauth_states",
            "classroom_course_mappings",
            "classroom_roster_identities",
        } <= tables
        assert backup.execute("SELECT COUNT(*) FROM classroom_roster_identities").fetchone()[0] == 1

    erased = asyncio.run(
        erase_user_data(2, {"id": 1, "role": "admin", "group_id": 1}, classroom_db)
    )
    assert erased["status"] == "ok"
    assert classroom_db.execute(text("SELECT COUNT(*) FROM classroom_roster_identities WHERE user_id=2")).scalar_one() == 0
    assert classroom_db.execute(text("SELECT state FROM education_cost_center_grants WHERE user_id=2")).scalar_one() == "revoked"
    connection = classroom_db.execute(text("SELECT * FROM classroom_connections WHERE org_id=1")).one()
    assert connection.state == "not_connected"
    assert connection.account_email is None
    assert connection.refresh_token_encrypted is None
    assert classroom_db.execute(text("SELECT COUNT(*) FROM classroom_course_mappings")).scalar_one() == 1
