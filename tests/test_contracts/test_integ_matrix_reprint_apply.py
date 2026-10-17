"""Review fix O2: reprint creates a job (jobs.create); applying a profile changes a printer
(printers.edit). Both were matrix-exempt with an operator floor, so narrowing those keys in
Settings > Permissions did not stop operators."""
from tests.test_contracts.test_cert_G_harness import run_probe

PROBE = r'''
install_license("pro")
seed_user("o2-admin", "admin"); seed_user("o2-operator", "operator")
admin = login("o2-admin")
matrix = admin.get("/api/permissions").json()
narrowed = {**matrix["action_access"], "jobs.create": ["admin"], "printers.edit": ["admin"]}
r = admin.put("/api/permissions", json={"page_access": matrix["page_access"], "action_access": narrowed})
assert r.status_code == 200, r.text
op = login("o2-operator")
OUT["reprint"] = op.post("/api/archives/999/reprint", json={"printer_id": 1}).status_code
OUT["apply"] = op.post("/api/profiles/999/apply", json={"printer_id": 1}).status_code
r = admin.put("/api/permissions", json={"page_access": matrix["page_access"], "action_access": matrix["action_access"]})
assert r.status_code == 200, r.text
op = login("o2-operator")
OUT["reprint_default"] = op.post("/api/archives/999/reprint", json={"printer_id": 1}).status_code
OUT["apply_default"] = op.post("/api/profiles/999/apply", json={"printer_id": 1}).status_code
'''


def test_narrowed_keys_stop_operator_reprint_and_profile_apply(tmp_path_factory):
    out = run_probe("o2-matrix", PROBE, tmp_path_factory.mktemp("o2-matrix"))
    assert out["reprint"] == 403 and out["apply"] == 403, out
    # default matrix: operators hold both keys, so the handler runs (missing archive/profile)
    assert out["reprint_default"] in (404, 422) and out["apply_default"] in (404, 422), out


def test_reprint_and_apply_are_mapped_not_exempt():
    from core.rbac_routes import ACTION_ROUTES, write_exemption
    assert ("POST", "/api/archives/{archive_id}/reprint") in ACTION_ROUTES["jobs.create"]
    assert ("POST", "/api/profiles/{profile_id}/apply") in ACTION_ROUTES["printers.edit"]
    assert write_exemption("POST", "/api/archives/{archive_id}/reprint") is None
    assert write_exemption("POST", "/api/profiles/{profile_id}/apply") is None
