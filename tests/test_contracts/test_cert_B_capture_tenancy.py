"""Cert B timelapse capture fixes, in process (real daemon functions, bootstrapped SQLite schema).

* the daemon selects printing printers of every protocol (RUNNING and PRINTING) (F02-2.7-02)
* on an install without organizations an org-less, unshared printer's capture belongs to
  the install; with organizations it stays unresolved (no frames), and an organization
  created mid-print stops capture (F02-2.7-02)
"""
import pytest
from sqlalchemy import text

from tests.test_contracts.cert_B_support import no_organizations
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


# --------------------------------------------------------------------------- timelapse capture tenancy

@pytest.fixture
def capture(review_db, tmp_path, monkeypatch):
    from modules.archives import timelapse_capture
    monkeypatch.setattr(timelapse_capture, "TIMELAPSE_DIR", tmp_path)
    review_db.execute(text("INSERT INTO printers(id,name,model,is_active,timelapse_enabled,shared,org_id,gcode_state) VALUES "
                           "(31,'Org-less X1C','X1C',1,1,0,NULL,'RUNNING')"))
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(131,'benchy.3mf','benchy.3mf','/owned/benchy.3mf',NULL)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,print_file_id,status,quantity,hold) VALUES(431,'Benchy',31,131,'printing',1,0)"))
    review_db.commit()
    return timelapse_capture


def _anchor(db, tid):
    return tuple(db.execute(text("SELECT a.owner_state,a.owner_org_id,a.capture_printer_org_id,a.capture_printer_shared,a.ownership_source "
                                 "FROM timelapses t JOIN timelapse_ownership_anchors a ON a.operation_id=t.ownership_operation_id "
                                 "WHERE t.id=:id"), {"id": tid}).one())


def test_multi_tenant_org_less_unshared_capture_stays_unresolved(review_db, capture):
    tid = capture.get_or_create_timelapse(review_db, 31, 431)
    assert _anchor(review_db, tid) == ("unresolved", None, None, 0, "capture_unresolved")


def _visible(db, principal):
    from core.timelapse_access import timelapse_visibility
    condition, params = timelapse_visibility(principal)
    return db.execute(text("SELECT id FROM timelapses WHERE " + condition), params).scalars().all()


def test_single_tenant_capture_is_owned_by_the_install_only_while_no_organization_exists(review_db, capture):
    no_organizations(review_db)
    ticket = capture.get_or_create_timelapse(review_db, 31, 431, return_ticket=True)
    # 'shared' at capture (the printer was install-wide; the pinned publication contract
    # admits only known/shared), but the audience is re-decided at every read.
    assert _anchor(review_db, ticket.timelapse_id) == ("shared", None, None, 1, "first_capture_job_file")
    viewer = {"id": 1, "role": "viewer", "group_id": 1}
    foreign = {"id": 4, "role": "viewer", "group_id": 2}
    assert _visible(review_db, viewer) == [ticket.timelapse_id] == _visible(review_db, foreign)
    # Frames are admitted while the install is single-tenant...
    from modules.archives.timelapse_frame_capture import _capture_authority
    _capture_authority(review_db, nonce=ticket.nonce, timelapse_id=ticket.timelapse_id, printer_id=31, job_id=431)
    review_db.rollback()
    # ...and once organizations exist (created after the capture), the recording is
    # admin-only and capture stops.
    # Same second as the capture: the stored fact decides, not timestamps.
    review_db.execute(text("UPDATE groups SET is_org=1,created_at=CURRENT_TIMESTAMP"))
    review_db.commit()
    assert _visible(review_db, foreign) == [] and _visible(review_db, viewer) == []
    assert _visible(review_db, {"id": 3, "role": "admin", "group_id": None}) == [ticket.timelapse_id]
    with pytest.raises(ValueError, match="Capture authority changed"):
        _capture_authority(review_db, nonce=ticket.nonce, timelapse_id=ticket.timelapse_id, printer_id=31, job_id=431)
    review_db.rollback()


def test_capture_of_an_explicitly_shared_printer_keeps_its_audience(review_db, capture):
    review_db.execute(text("UPDATE printers SET shared=1 WHERE id=31"))
    review_db.commit()
    tid = capture.get_or_create_timelapse(review_db, 31, 431)  # multi-org install, explicitly shared printer
    foreign = {"id": 4, "role": "viewer", "group_id": 2}
    assert _visible(review_db, foreign) == [tid]
    # Captured while organizations existed: the capture-time sharing is immutable.
    review_db.execute(text("UPDATE printers SET shared=0,org_id=1 WHERE id=31"))
    review_db.commit()
    assert _visible(review_db, foreign) == [tid]


def test_single_tenant_rule_never_widens_an_org_printer(review_db, capture):
    no_organizations(review_db)
    review_db.execute(text("UPDATE printers SET org_id=1 WHERE id=31"))
    review_db.commit()
    tid = capture.get_or_create_timelapse(review_db, 31, 431)
    assert _anchor(review_db, tid)[0] == "unresolved"  # org-bound printer, org-less content: not install-wide


def test_daemon_selects_printing_printers_of_every_protocol(review_db, capture):
    review_db.execute(text("INSERT INTO printers(id,name,is_active,timelapse_enabled,gcode_state) VALUES "
                           "(32,'Klipper',1,1,'PRINTING'),(33,'Paused Klipper',1,1,'PAUSED'),(34,'Paused Bambu',1,1,'PAUSE'),"
                           "(35,'Idle',1,1,'IDLE'),(36,'Disabled',1,0,'PRINTING')"))
    review_db.commit()
    assert sorted(p["id"] for p in capture.get_active_printers(review_db) if p["id"] >= 31) == [31, 32]




def _fact(db, nonce):
    return db.execute(text("SELECT captured_single_tenant FROM timelapse_capture_tenancy WHERE operation_id=:n"), {"n": nonce}).scalar()


def test_capture_records_the_tenancy_fact_once(review_db, capture):
    single = capture.get_or_create_timelapse(review_db, 31, 431, return_ticket=True)
    assert _fact(review_db, single.nonce) in (0, False)  # this install has organizations
    no_organizations(review_db)
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=431"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,print_file_id,status,quantity,hold) VALUES(432,'B2',31,131,'printing',1,0)"))
    review_db.commit()
    later = capture.get_or_create_timelapse(review_db, 31, 432, return_ticket=True)
    assert _fact(review_db, later.nonce) in (1, True)
    assert review_db.execute(text("SELECT COUNT(*) FROM timelapse_capture_tenancy")).scalar_one() == 2


def test_anchor_without_a_fact_fails_closed_once_organizations_exist(review_db, capture):
    """Captures from before migration 033 have no row: install-wide only without organizations."""
    no_organizations(review_db)
    ticket = capture.get_or_create_timelapse(review_db, 31, 431, return_ticket=True)
    review_db.execute(text("DELETE FROM timelapse_capture_tenancy")); review_db.commit()
    viewer = {"id": 1, "role": "viewer", "group_id": 1}
    assert _visible(review_db, viewer) == [ticket.timelapse_id]
    review_db.execute(text("UPDATE groups SET is_org=1")); review_db.commit()
    assert _visible(review_db, viewer) == []
    assert _visible(review_db, {"id": 3, "role": "admin", "group_id": None}) == [ticket.timelapse_id]


def test_inherit_copies_the_fact_to_the_derived_anchor(review_db, capture):
    from core.timelapse_access import inherit_timelapse_owner
    from modules.archives.models import Timelapse
    no_organizations(review_db)
    ticket = capture.get_or_create_timelapse(review_db, 31, 431, return_ticket=True)
    source = review_db.get(Timelapse, ticket.timelapse_id)
    derived = Timelapse(printer_id=31, print_job_id=431, filename="printer_31/derived.mp4", status="encoding", frame_count=0)
    review_db.add(derived); review_db.flush()
    inherit_timelapse_owner(review_db, source, derived, {"id": 3, "role": "admin", "group_id": None})
    review_db.commit()
    assert _fact(review_db, derived.ownership_operation_id) in (1, True)
    # The derived media follows the source's audience once organizations exist.
    review_db.execute(text("UPDATE groups SET is_org=1")); review_db.commit()
    assert _visible(review_db, {"id": 4, "role": "viewer", "group_id": 2}) == []


def test_organization_created_between_the_two_decisions_cannot_split_them(review_db, capture, monkeypatch):
    """The owner state and the stored fact come from one tenancy observation."""
    from core import tenancy_mode
    no_organizations(review_db)
    real = tenancy_mode.install_has_organizations
    calls = []

    def observe_then_create_org(connection):
        seen = real(connection)
        calls.append(seen)
        if len(calls) == 1:  # an organization appears right after the first observation
            connection.execute(text("UPDATE groups SET is_org=1 WHERE id=1"))
        return seen

    monkeypatch.setattr(tenancy_mode, "install_has_organizations", observe_then_create_org)
    ticket = capture.get_or_create_timelapse(review_db, 31, 431, return_ticket=True)
    monkeypatch.setattr(tenancy_mode, "install_has_organizations", real)
    assert calls == [False]
    # 'shared' because the install was single-tenant when observed, and the fact agrees.
    assert _anchor(review_db, ticket.timelapse_id)[0] == "shared"
    fact = review_db.execute(text("SELECT captured_single_tenant FROM timelapse_capture_tenancy WHERE operation_id=:n"),
                             {"n": ticket.nonce}).scalar_one()
    assert fact in (True, 1)
    # Organizations now exist, so the single-tenant capture is admin-only (fail closed).
    assert _visible(review_db, {"id": 4, "role": "viewer", "group_id": 2}) == []
