"""Dashboard totals: a print ODIN dispatched is a job AND a tracked print; it is one print."""
from sqlalchemy import text

from tests.test_contracts.test_cert_E_ana_backend import ana, _FrozenDatetime  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_product_bom_integrity import bom_client  # noqa: F401


def _printing(client):
    return client.get("/stats", params={"days": 7}).json()["jobs"]


def test_a_dispatched_print_is_counted_once_and_a_hand_started_one_still_counts(ana):
    client, _identity, db = ana
    before = _printing(client)
    db.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,quantity,priority,created_at,charged_to_org_id,hold) "
                    "VALUES (7101,'Dispatched by ODIN','printing',9,1,3,'2026-10-12 08:00:00',1,0)"))
    # the same print as the monitor tracks it, linked to its job
    db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,scheduled_job_id) "
                    "VALUES (9,'Dispatched by ODIN.3mf','running','2026-10-12 08:01:00',7101)"))
    # a print started at the printer: tracked, with no ODIN job
    db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at) "
                    "VALUES (9,'Started by hand.3mf','running','2026-10-12 08:05:00')"))
    db.commit()
    after = _printing(client)
    assert after["printing"] == before["printing"] + 2

    # finishing both: the dispatched one is one completed print, not two
    db.execute(text("UPDATE jobs SET status='completed', actual_end=:end WHERE id=7101"), {"end": _FrozenDatetime.now().replace(tzinfo=None)})
    db.execute(text("UPDATE print_jobs SET status='completed', ended_at=:end"), {"end": _FrozenDatetime.now().replace(tzinfo=None)})
    db.commit()
    done = _printing(client)
    assert done["printing"] == before["printing"]
    assert done["completed_today"] == before["completed_today"] + 2
