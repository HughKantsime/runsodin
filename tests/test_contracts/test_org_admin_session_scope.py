"""Register B69: an organization admin must not see sessions of users outside the organization.

A user with no organization can be a platform admin; listing those sessions hands an
organization admin the platform admin's username, IP address and browser.
"""
from sqlalchemy import text

from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def _session(db, user_id, username, role, device):
    import jwt
    from core import auth
    from core.auth import create_access_token
    from core.session_tracking import record_session

    token = create_access_token({"sub": username, "role": role})
    assert record_session(db, user_id, token, "198.51.100.7", device) is True
    jti = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])["jti"]
    session_id = db.execute(
        text("SELECT id FROM active_sessions WHERE token_jti=:jti"), {"jti": jti}
    ).scalar_one()
    return {"Authorization": "Bearer " + token}, session_id


def _add_unassigned_users(db):
    db.execute(text(
        "INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) VALUES "
        "(60,'platform','platform@example.test','fixture','admin',1,NULL),"
        "(61,'loose','loose@example.test','fixture','viewer',1,NULL)"
    ))
    db.commit()


def test_org_admin_session_list_excludes_users_without_an_organization(token_client, review_db):
    client, _ = token_client
    _add_unassigned_users(review_db)
    org_admin, own = _session(review_db, 3, "admin", "admin", "Org admin browser")
    _, member = _session(review_db, 1, "student", "viewer", "Member browser")
    _, platform = _session(review_db, 60, "platform", "admin", "Platform admin browser")
    _, loose = _session(review_db, 61, "loose", "viewer", "Unassigned browser")
    _, foreign = _session(review_db, 5, "foreign", "admin", "Other org browser")

    response = client.get("/admin/sessions", headers=org_admin)
    assert response.status_code == 200
    rows = response.json()
    assert {r["id"] for r in rows} == {own, member}
    assert not {"platform", "loose", "foreign"} & {r["username"] for r in rows}


def test_platform_admin_session_list_still_shows_everyone(token_client, review_db):
    client, _ = token_client
    _add_unassigned_users(review_db)
    _, member = _session(review_db, 1, "student", "viewer", "Member browser")
    platform_admin, platform = _session(review_db, 60, "platform", "admin", "Platform admin browser")
    _, foreign = _session(review_db, 5, "foreign", "admin", "Other org browser")

    response = client.get("/admin/sessions", headers=platform_admin)
    assert response.status_code == 200
    assert {r["id"] for r in response.json()} == {member, platform, foreign}


def test_org_admin_cannot_revoke_a_platform_admin_session(token_client, review_db):
    client, _ = token_client
    _add_unassigned_users(review_db)
    org_admin, _ = _session(review_db, 3, "admin", "admin", "Org admin browser")
    platform_admin, platform = _session(review_db, 60, "platform", "admin", "Platform admin browser")

    assert client.delete(f"/admin/sessions/{platform}", headers=org_admin).status_code == 403
    assert client.get("/sessions", headers=platform_admin).status_code == 200
