"""Cert B archive fixes, in process (real routers, bootstrapped SQLite schema).

* AMS mapping preview for a target printer with slots (F02-2.8-26/29)
* unresolved archive ownership resolves to the install only while it has no
  organizations; multi-tenant isolation stays fail-closed (F02-2.8-11/15/16)
"""
import pytest
from sqlalchemy import text

from tests.test_contracts.cert_B_support import check_wire, no_organizations
from tests.test_contracts.test_archive_reprint_behavior import reprint_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


# --------------------------------------------------------------------------- AMS preview

def test_ams_preview_for_printer_with_slots_maps_distinct_trays(reprint_client, review_db):
    client, identity, _ = reprint_client
    review_db.execute(text("UPDATE print_files SET filaments_json=:f WHERE id=101"),
                      {"f": '[{"type":"PLA","color":"#000000"},{"type":"PLA","color":"#FFFFFF"},{"type":"PETG","color":"#FF0000"}]'})
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(502,'Synthetic','Black','PLA')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,remaining_weight_g,initial_weight_g) VALUES(602,502,500,1000)"))
    review_db.execute(text("DELETE FROM filament_slots WHERE printer_id=9"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color,color_hex,assigned_spool_id) VALUES"
                           "(9,1,'PLA','White','FFFFFF',NULL),(9,2,'PLA','Black','000000',602),(9,3,'ABS','Red','FF0000',NULL)"))
    review_db.commit()
    identity.update(role="viewer")
    response = client.get("/archives/101/ams-preview?printer_id=9")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [(s["slot"], s["spool_id"]) for s in body["available"]] == [(1, None), (2, 602), (3, None)]
    assert body["suggested_mapping"] == [
        {"source_slot": 0, "target_slot": 2, "confidence": "exact"},  # black PLA by hex
        {"source_slot": 1, "target_slot": 1, "confidence": "exact"},  # white PLA, tray not reused
    ]  # PETG has no tray
    check_wire("archive_ams_preview", body)
    assert client.get("/archives/101/ams-preview?printer_id=11").status_code == 404  # foreign printer


# --------------------------------------------------------------------------- archive tenancy

@pytest.fixture
def archive_rows(reprint_client, review_db):
    client, identity, _ = reprint_client
    review_db.execute(text("INSERT INTO print_archives(id,printer_id,print_name,status,org_id,ownership_state,ownership_source) VALUES "
                           "(201,9,'Printer-started print','completed',NULL,'unresolved','missing_job_file'),"
                           "(202,9,'Foreign tenant print','completed',2,'known','job_file'),"
                           "(203,9,'Changed source print','completed',2,'unresolved','source_graph_changed')"))
    review_db.commit()
    identity.update(role="viewer", group_id=1)

    def names():
        response = client.get("/archives?per_page=200")
        assert response.status_code == 200, response.text
        return {a["print_name"] for a in response.json()["items"]}
    return client, identity, names


def test_multi_tenant_install_keeps_unresolved_archives_admin_only(archive_rows):
    client, identity, names = archive_rows
    visible = names()
    assert "Printer-started print" not in visible and "Foreign tenant print" not in visible
    assert client.get("/archives/201").status_code == 404
    identity.update(role="admin", group_id=None)
    assert {"Printer-started print", "Foreign tenant print", "Changed source print"} <= names()


def test_single_tenant_install_shows_unresolved_archives_by_role(archive_rows, review_db):
    client, identity, names = archive_rows
    no_organizations(review_db)
    for role in ("viewer", "operator"):
        identity.update(role=role, group_id=None)
        visible = names()
        assert "Printer-started print" in visible, role
        # Rows carrying another tenant's id are never admitted by the install rule.
        assert "Foreign tenant print" not in visible and "Changed source print" not in visible
        assert client.get("/archives/201").status_code == 200
        assert client.get("/archives/log?per_page=200").json()["total"] >= 1
    identity.update(role="viewer")
    assert client.patch("/archives/201", json={"notes": "x"}).status_code == 403  # roles still apply
    # Creating the first organization returns unresolved history to admin-only.
    review_db.execute(text("UPDATE groups SET is_org=1 WHERE id=1"))
    review_db.commit()
    assert "Printer-started print" not in names()


