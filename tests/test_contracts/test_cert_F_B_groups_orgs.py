"""Cert F / cluster B — F11-11.4 (User Groups) and F11-11.5 (Organizations) on the real server (LiveOdin).

Backend halves of: "Group management UI for organizing users", "Group owner concept for targeted approval alert
routing", "Create organizations to group users and assign printer/model/spool resources", "Member management
(add/remove users) and printer assignment within organizations", "Organization management UI in Settings -> Access".
Wire captures for the UI cert tests are written to artifacts/application-certification/cert-F/B-wire-captures.json.
"""
from __future__ import annotations

import pytest

from tests.test_contracts.test_cert_F_B_00_support import LiveOdinB
from tests.test_contracts.test_cert_F_B_api_tokens import capture


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdinB(tmp_path_factory.mktemp("groups")).start()
    o.boot_admin()
    o.owner = o.mkuser("grp_owner", "operator")
    o.other_op = o.mkuser("grp_other_operator", "operator")
    o.student = o.mkuser("grp_student", "viewer")
    o.other_admin_id = o.mkuser("grp_second_admin", "admin", login=False)["id"]
    yield o
    o.stop()


def alerts_for(o, job_id):
    return {r.user_id for r in o.all("SELECT user_id FROM alerts WHERE alert_type='job_submitted' AND job_id=:j", j=job_id)}


# ------------------------------------------------------------------ F11-11.4-01 group management
def test_admin_creates_lists_edits_and_deletes_groups_with_members(odin):
    a = odin.admin_bearer
    r = odin.post("/api/groups", json={"name": "Period 3", "description": "Engineering", "owner_id": odin.owner["id"]}, headers=a)
    assert r.status_code == 200 and r.json()["status"] == "created", r.text
    gid = r.json()["id"]
    assert odin.patch(f"/api/users/{odin.student['id']}", json={"group_id": gid}, headers=a).status_code == 200
    listed = [g for g in odin.get("/api/groups", headers=a).json() if g["id"] == gid][0]
    assert listed["owner_username"] == "grp_owner" and listed["member_count"] == 1 and listed["description"] == "Engineering"
    detail = odin.get(f"/api/groups/{gid}", headers=a).json()
    assert [m["username"] for m in detail["members"]] == ["grp_student"]
    capture("groups_list", odin.get("/api/groups", headers=a).json())
    capture("users_list", odin.get("/api/users", headers=a).json())
    assert odin.patch(f"/api/groups/{gid}", json={"name": "Period 3B"}, headers=a).status_code == 200
    assert odin.one("SELECT name FROM groups WHERE id=:i", i=gid).name == "Period 3B"
    # Non-admins cannot manage groups.
    assert odin.post("/api/groups", json={"name": "Rogue"}, headers=odin.owner["headers"]).status_code == 403
    assert odin.patch(f"/api/groups/{gid}", json={"name": "Rogue"}, headers=odin.owner["headers"]).status_code == 403
    temp = odin.post("/api/groups", json={"name": "Temporary"}, headers=a).json()["id"]
    assert odin.delete(f"/api/groups/{temp}", headers=a).status_code == 200
    assert odin.one("SELECT 1 FROM groups WHERE id=:i", i=temp) is None
    odin.group_id = gid


def test_group_owner_must_be_operator_or_admin(odin):
    r = odin.post("/api/groups", json={"name": "Viewer owned", "owner_id": odin.student["id"]}, headers=odin.admin_bearer)
    assert r.status_code == 400
    assert odin.one("SELECT 1 FROM groups WHERE name='Viewer owned'") is None


# ------------------------------------------------------------------ F11-11.4-02 owner-targeted approval alerts
@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: backend/modules/jobs/routes/jobs_crud.py:499-500 + backend/core/recipient_policy.py:21-22 — when a group owner "
    "is set, the JOB_SUBMITTED alert targets only the owner, then resource scoping drops the owner unless the owner is a "
    "member of the job's group (or a superadmin), and there is no fallback. Scenario: an admin creates 'Period 3' and picks "
    "teacher operator T as owner (GroupManager lists every operator; POST /api/groups accepts any active operator), but T is "
    "not a member; a student submits a job in approval mode -> zero alert rows are written, nobody is told, and the job "
    "sits in 'submitted'."))
def test_non_member_operator_owner_still_receives_approval_alert(odin):
    a = odin.admin_bearer
    assert odin.put("/api/config/require-job-approval", json={"enabled": True}, headers=a).status_code == 200
    r = odin.post("/api/jobs", json={"item_name": "Student bracket (owner outside group)"}, headers=odin.student["headers"])
    assert r.status_code == 201, r.text
    assert odin.one("SELECT status FROM jobs WHERE id=:i", i=r.json()["id"]).status == "submitted"
    assert alerts_for(odin, r.json()["id"]) == {odin.owner["id"]}


def test_member_submission_alerts_only_the_group_owner(odin):
    a = odin.admin_bearer
    assert odin.put("/api/config/require-job-approval", json={"enabled": True}, headers=a).status_code == 200
    # The owner is a member of the group (the tenant-safe configuration).
    assert odin.patch(f"/api/users/{odin.owner['id']}", json={"group_id": odin.group_id}, headers=a).status_code == 200
    r = odin.post("/api/jobs", json={"item_name": "Student bracket"}, headers=odin.student["headers"])
    assert r.status_code == 201, r.text
    job = r.json()["id"]
    assert odin.one("SELECT status FROM jobs WHERE id=:i", i=job).status == "submitted"
    # Only the owner: not the unrelated operator, not the two admins.
    assert alerts_for(odin, job) == {odin.owner["id"]}
    owner_feed = odin.get("/api/alerts", headers=odin.owner["headers"]).json()
    other_feed = odin.get("/api/alerts", headers=odin.other_op["headers"]).json()
    items = lambda feed: feed if isinstance(feed, list) else feed.get("alerts", feed.get("items", []))
    assert any(x.get("job_id") == job for x in items(owner_feed))
    assert not any(x.get("job_id") == job for x in items(other_feed))


def test_without_owner_alerts_fall_back_to_admins_not_unrelated_operators(odin):
    a = odin.admin_bearer
    assert odin.patch(f"/api/groups/{odin.group_id}", json={"owner_id": None}, headers=a).status_code == 200
    r = odin.post("/api/jobs", json={"item_name": "Student fallback"}, headers=odin.student["headers"])
    assert r.status_code == 201, r.text
    recipients = alerts_for(odin, r.json()["id"])
    # Fallback: active admins plus operators inside the group (the former owner is a member operator).
    assert odin.other_op["id"] not in recipients
    assert recipients == {1, odin.other_admin_id, odin.owner["id"]}
    odin.put("/api/config/require-job-approval", json={"enabled": False}, headers=a)


# ------------------------------------------------------------------ F11-11.5 organizations
@pytest.fixture(scope="module")
def orgs(odin):
    a = odin.admin_bearer
    org_a = odin.post("/api/orgs", json={"name": "Org Alpha", "description": "Tenant A"}, headers=a)
    org_b = odin.post("/api/orgs", json={"name": "Org Beta"}, headers=a)
    assert org_a.status_code == 200 and org_b.status_code == 200, (org_a.text, org_b.text)
    alpha = odin.mkuser("org_alpha_op", "operator")
    beta = odin.mkuser("org_beta_op", "operator")
    p1 = odin.post("/api/printers", json={"name": "Alpha printer", "api_type": "moonraker", "api_host": "192.0.2.41"}, headers=a).json()["id"]
    p2 = odin.post("/api/printers", json={"name": "Shared floor printer", "api_type": "moonraker", "api_host": "192.0.2.42"}, headers=a).json()["id"]
    return {"a": org_a.json()["id"], "b": org_b.json()["id"], "alpha": alpha, "beta": beta, "p1": p1, "p2": p2}


def test_superadmin_creates_organizations_and_non_admins_cannot(odin, orgs):
    listed = {o["name"]: o for o in odin.get("/api/orgs", headers=odin.admin_bearer).json()}
    assert {"Org Alpha", "Org Beta"} <= set(listed)
    assert listed["Org Alpha"]["description"] == "Tenant A"
    assert odin.one("SELECT is_org FROM groups WHERE id=:i", i=orgs["a"]).is_org
    assert odin.post("/api/orgs", json={"name": "Rogue org"}, headers=orgs["alpha"]["headers"]).status_code == 403
    assert odin.post("/api/orgs", json={"name": "Org Alpha"}, headers=odin.admin_bearer).status_code == 409


def test_member_add_and_remove(odin, orgs):
    a = odin.admin_bearer
    for key, org in (("alpha", "a"), ("beta", "b")):
        r = odin.post(f"/api/orgs/{orgs[org]}/members", json={"user_id": orgs[key]["id"]}, headers=a)
        assert r.status_code == 200, r.text
        assert odin.one("SELECT group_id FROM users WHERE id=:i", i=orgs[key]["id"]).group_id == orgs[org]
    assert {o["name"]: o["member_count"] for o in odin.get("/api/orgs", headers=a).json()}["Org Alpha"] == 1
    # Removal: membership is users.group_id, cleared through the user editor.
    extra = odin.mkuser("org_temp_member", "viewer", login=False)
    assert odin.post(f"/api/orgs/{orgs['a']}/members", json={"user_id": extra["id"]}, headers=a).status_code == 200
    assert {o["name"]: o["member_count"] for o in odin.get("/api/orgs", headers=a).json()}["Org Alpha"] == 2
    assert odin.patch(f"/api/users/{extra['id']}", json={"group_id": None}, headers=a).status_code == 200
    assert odin.one("SELECT group_id FROM users WHERE id=:i", i=extra["id"]).group_id is None
    assert {o["name"]: o["member_count"] for o in odin.get("/api/orgs", headers=a).json()}["Org Alpha"] == 1
    # Non-superadmins cannot add members.
    assert odin.post(f"/api/orgs/{orgs['a']}/members", json={"user_id": extra["id"]}, headers=orgs["alpha"]["headers"]).status_code == 403
    capture("orgs_list", odin.get("/api/orgs", headers=a).json())
    capture("printers_list", odin.get("/api/printers", headers=a).json())
    capture("groups_list_with_orgs", odin.get("/api/groups", headers=a).json())
    capture("users_list_with_orgs", odin.get("/api/users", headers=a).json())
    capture("license", odin.get("/api/license").json())
    capture("permissions", odin.get("/api/permissions").json())
    capture("auth_me_admin", odin.get("/api/auth/me", headers=a).json())


def test_printer_assignment_scopes_visibility(odin, orgs):
    a = odin.admin_bearer
    r = odin.post(f"/api/orgs/{orgs['a']}/printers", json={"printer_id": orgs["p1"]}, headers=a)
    assert r.status_code == 200, r.text
    assert odin.one("SELECT org_id FROM printers WHERE id=:i", i=orgs["p1"]).org_id == orgs["a"]
    assert odin.post(f"/api/orgs/{orgs['b']}/printers", json={"printer_id": orgs["p2"]}, headers=orgs["alpha"]["headers"]).status_code == 403
    alpha_names = {p["name"] for p in odin.get("/api/printers", headers=orgs["alpha"]["headers"]).json()}
    beta_names = {p["name"] for p in odin.get("/api/printers", headers=orgs["beta"]["headers"]).json()}
    assert "Alpha printer" in alpha_names and "Alpha printer" not in beta_names
    assert "Shared floor printer" in alpha_names and "Shared floor printer" in beta_names  # unassigned = visible to all
    assert odin.get(f"/api/printers/{orgs['p1']}", headers=orgs["beta"]["headers"]).status_code == 404


def test_models_and_spools_created_by_members_belong_to_their_org(odin, orgs):
    alpha, beta = orgs["alpha"]["headers"], orgs["beta"]["headers"]
    model = odin.post("/api/models", json={"name": "Alpha bracket"}, headers=alpha).json()["id"]
    fil = odin.post("/api/filaments", json={"brand": "Cert", "name": "Org PLA"}, headers=alpha).json()["id"]
    spool = odin.post("/api/spools", json={"filament_id": fil}, headers=alpha).json()["id"]
    assert odin.one("SELECT org_id FROM models WHERE id=:i", i=model).org_id == orgs["a"]
    assert odin.one("SELECT org_id FROM spools WHERE id=:i", i=spool).org_id == orgs["a"]
    assert model not in {m["id"] for m in odin.get("/api/models", headers=beta).json()}
    assert spool not in {s["id"] for s in odin.get("/api/spools", headers=beta).json()}
    assert odin.get(f"/api/models/{model}", headers=beta).status_code == 404
    assert odin.get(f"/api/spools/{spool}", headers=beta).status_code == 404
    assert model in {m["id"] for m in odin.get("/api/models", headers=alpha).json()}
    odin.org_model, odin.org_spool = model, spool


def test_no_api_assigns_an_existing_model_or_spool_to_an_org(odin, orgs):
    """Evidence for the claim_mismatch: only printers have an assignment endpoint; model/spool ownership
    comes from the creating member's organization and an admin PATCH carrying org_id does not move them."""
    a = odin.admin_bearer
    model = odin.post("/api/models", json={"name": "Platform model"}, headers=a).json()["id"]
    assert odin.one("SELECT org_id FROM models WHERE id=:i", i=model).org_id is None
    odin.patch(f"/api/models/{model}", json={"org_id": orgs["b"]}, headers=a)
    assert odin.one("SELECT org_id FROM models WHERE id=:i", i=model).org_id is None
    for path in (f"/api/orgs/{orgs['b']}/models", f"/api/orgs/{orgs['b']}/spools"):
        assert odin.post(path, json={"model_id": model, "spool_id": odin.org_spool}, headers=a).status_code in (404, 405)


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: backend/modules/organizations/routes.py:152-154 — DELETE /api/orgs/{id} unlinks resources with "
    "UPDATE spools SET org_id=NULL, which the economic-source trigger from backend/core/schema/migrations/"
    "022_economic_source_lifetimes.py:56-62 forbids (spool org_id is immutable once born), so the request fails with "
    "an unhandled IntegrityError -> HTTP 500. Scenario: a superadmin deletes an organization in which any member ever "
    "created a spool; the delete always returns 'Internal server error' and the organization cannot be removed."))
def test_org_delete_unlinks_members_and_resources(odin, orgs):
    a = odin.admin_bearer
    r = odin.delete(f"/api/orgs/{orgs['a']}", headers=a)
    assert r.status_code == 200, r.text
    assert odin.one("SELECT group_id FROM users WHERE id=:i", i=orgs["alpha"]["id"]).group_id is None
    assert odin.one("SELECT org_id FROM printers WHERE id=:i", i=orgs["p1"]).org_id is None
    assert odin.one("SELECT org_id FROM models WHERE id=:i", i=odin.org_model).org_id is None
