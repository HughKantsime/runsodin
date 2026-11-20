"""Register B69: an organization admin must not see sessions of users outside the organization.

A user with no organization can be a platform admin; listing those sessions hands an
organization admin the platform admin's username, IP address and browser.
"""
import pytest
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


@pytest.mark.parametrize("path", ["/users", "/admin/quotas"])
def test_admin_user_lists_respect_organization_membership(token_client, review_db, education_license, path):
    from modules.organizations.routes_users import router

    client, _ = token_client
    client.app.include_router(router)
    _add_unassigned_users(review_db)
    org_admin, _ = _session(review_db, 3, "admin", "admin", "Org browser")
    platform_admin, _ = _session(review_db, 60, "platform", "admin", "Platform browser")
    own = {"student", "manager", "admin", "outsider"}
    everyone = own | {"foreign", "platform", "loose"}
    for headers, expected in [(org_admin, own), (platform_admin, everyone)]:
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert {row["username"] for row in response.json()} == expected


@pytest.fixture
def member_client(token_client, review_db):
    from modules.organizations.routes import router

    client, _ = token_client
    client.app.include_router(router)
    _add_unassigned_users(review_db)
    review_db.execute(text(
        "INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) VALUES "
        "(62,'inactive','inactive@example.test','fixture','viewer',0,NULL),"
        "(63,'operator','operator@example.test','fixture','operator',1,2),"
        "(64,'recorded','recorded@example.test','fixture','viewer',1,NULL)"
    ))
    review_db.execute(text(
        "INSERT INTO education_audit_events "
        "(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,resource_id) "
        "VALUES ('member-event',1,'user','64','review','member-command','hash','user','64')"
    ))
    review_db.commit()
    return client


@pytest.mark.parametrize(("admin_id", "username", "expected"), [
    (3, "admin", [(61, "loose")]),
    (60, "platform", [(5, "foreign"), (61, "loose"), (63, "operator")]),
])
def test_member_picker_only_lists_people_the_admin_can_add(
    member_client, review_db, education_license, admin_id, username, expected,
):
    headers, _ = _session(review_db, admin_id, username, "admin", "Browser")
    path = "/orgs/1/member-candidates"
    response = member_client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.json() == [{"id": uid, "username": name} for uid, name in expected]
    assert member_client.post("/orgs/1/members", headers=headers, json={"user_id": 64}).status_code == 409
    for person in response.json():
        added = member_client.post("/orgs/1/members", headers=headers, json={"user_id": person["id"]})
        assert added.status_code == 200
    assert member_client.get(path, headers=headers).json() == []


def test_member_candidates_require_admin_access_to_destination(member_client, review_db, education_license):
    foreign, _ = _session(review_db, 5, "foreign", "admin", "Browser")
    viewer, _ = _session(review_db, 1, "student", "viewer", "Browser")
    platform, _ = _session(review_db, 60, "platform", "admin", "Browser")
    for headers in (foreign, viewer):
        assert member_client.get("/orgs/1/member-candidates", headers=headers).status_code == 403
    assert member_client.get("/orgs/999/member-candidates", headers=platform).status_code == 404


def test_member_add_hides_ineligible_targets_before_checking_history(member_client, review_db, education_license):
    # Give an out-of-org user history as well as the unassigned user in the fixture.
    review_db.execute(text(
        "INSERT INTO education_audit_events "
        "(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,resource_id) "
        "VALUES ('foreign-event',2,'user','63','review','foreign-command','hash','user','63')"
    ))
    review_db.commit()
    org, _ = _session(review_db, 3, "admin", "admin", "Browser")
    platform, _ = _session(review_db, 60, "platform", "admin", "Browser")

    def add(headers, uid):
        return member_client.post("/orgs/1/members", headers=headers, json={"user_id": uid})

    unknown = add(org, 999)
    assert unknown.status_code == 404 and unknown.json() == {"detail": "User not found"}
    for uid in (60, 5, 63):
        refused = add(org, uid)
        assert (refused.status_code, refused.content) == (unknown.status_code, unknown.content)
    assert add(org, 64).status_code == 409  # Otherwise eligible.
    assert add(platform, 63).status_code == 409
    assert add(platform, 5).status_code == 200


def test_member_candidates_require_user_groups(member_client, review_db, tier_license):
    tier_license(None)
    headers, _ = _session(review_db, 3, "admin", "admin", "Browser")
    response = member_client.get("/orgs/1/member-candidates", headers=headers)
    assert response.status_code == 403
    assert response.headers["X-ODIN-License-Feature"] == "user_groups"


@pytest.fixture
def group_client(member_client, review_db):
    from modules.organizations.routes_users import router

    member_client.app.include_router(router)
    review_db.execute(text("UPDATE users SET role='operator',group_id=1 WHERE id IN (62,63)"))
    review_db.commit()
    return member_client


@pytest.mark.parametrize("platform", [False, True])
def test_group_reads_follow_organization_scope(group_client, review_db, education_license, platform):
    uid, name = (60, "platform") if platform else (3, "admin")
    headers, _ = _session(review_db, uid, name, "admin", "Browser")
    listed = group_client.get("/groups", headers=headers)
    assert listed.status_code == 200
    assert {g["id"] for g in listed.json()} == ({1, 2} if platform else {1})
    own = group_client.get("/groups/1", headers=headers)
    assert own.status_code == 200
    assert {u["id"] for u in own.json()["members"]} == {1, 2, 3, 4, 62, 63}
    other = group_client.get("/groups/2", headers=headers)
    if platform:
        assert other.status_code == 200
        assert [u["id"] for u in other.json()["members"]] == [5]
    else:
        assert other.status_code == 403
        assert other.json()["detail"] == "Access denied"


@pytest.mark.parametrize("resource", ["groups", "orgs"])
def test_owner_selection_respects_admin_scope(group_client, review_db, education_license, resource):
    org, _ = _session(review_db, 3, "admin", "admin", "Browser")
    platform, _ = _session(review_db, 60, "platform", "admin", "Browser")
    other = group_client.patch(f"/{resource}/2", headers=org, json={"owner_id": 5})
    assert other.status_code == (403 if resource == "groups" else 404)
    path = f"/{resource}/1"
    for owner in (63, 3, None):
        assert group_client.patch(path, headers=org, json={"owner_id": owner}).status_code == 200
    for owner in (60, 5, 62, 1, 0):
        refused = group_client.patch(path, headers=org, json={"owner_id": owner})
        assert refused.status_code == 400
        assert review_db.execute(text("SELECT owner_id FROM groups WHERE id=1")).scalar_one() is None
    assert group_client.patch(path, headers=platform, json={"owner_id": 5}).status_code == 200
    assert review_db.execute(text("SELECT owner_id FROM groups WHERE id=1")).scalar_one() == 5
