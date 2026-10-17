"""Review fix O10: toggling a printer's camera needs org ownership; sharing grants viewing only."""
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

OTHER_ORG_ADMIN = {"id": 50, "role": "admin", "group_id": 2, "is_active": True}
OWNER_ADMIN = {"id": 51, "role": "admin", "group_id": 1, "is_active": True}


def _shared_printer(db):
    db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Owner org',TRUE),(2,'Other org',TRUE) ON CONFLICT DO NOTHING"))
    db.execute(text("INSERT INTO printers(id,name,model,org_id,shared,is_active,camera_enabled) "
                    "VALUES(880,'Shared lab printer','X1C',1,TRUE,TRUE,TRUE)"))
    db.commit()


def _state(db):
    return db.execute(text("SELECT camera_enabled FROM printers WHERE id=880")).scalar_one()


def test_other_org_cannot_toggle_a_shared_printers_camera(review_db):  # noqa: F811
    from modules.printers.camera_routes import toggle_camera
    with Session(review_db.get_bind()) as db:
        _shared_printer(db)
        with pytest.raises(HTTPException) as refused:
            toggle_camera(880, OTHER_ORG_ADMIN, db)
        assert refused.value.status_code == 404
        assert bool(_state(db)) is True


def test_owner_org_toggles_its_shared_printers_camera(review_db):  # noqa: F811
    from modules.printers.camera_routes import toggle_camera
    with Session(review_db.get_bind()) as db:
        _shared_printer(db)
        assert toggle_camera(880, OWNER_ADMIN, db) == {"id": 880, "camera_enabled": False}
        assert bool(_state(db)) is False
