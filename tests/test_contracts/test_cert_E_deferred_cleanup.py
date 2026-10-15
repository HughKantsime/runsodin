"""032 cleanup: deferred deliveries are removed with their user and their org (no-op without the table)."""
import importlib

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_account_deletion_token_lifetime import deletion_case  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_license_user_limits import signed_license  # noqa: F401

MIGRATION = importlib.import_module("core.schema.migrations.032_quiet_hours_deferred_deliveries")


def _apply(db):
    with db.get_bind().connect() as conn:
        MIGRATION.apply(conn)
        conn.commit()


def _insert(db, row_id, user_id, org_id, scope, channel="email"):
    db.execute(text("INSERT INTO quiet_hours_deferred_deliveries(id,user_id,org_id,scope_key,channel,alert_type,severity,title) "
                    "VALUES(:i,:u,:o,:s,:c,'print_failed','critical','t')"),
               {"i": row_id, "u": user_id, "o": org_id, "s": scope, "c": channel})


def test_user_deletion_removes_that_users_deferred_deliveries(deletion_case):  # noqa: F811
    client, owner, db, _ = deletion_case
    _apply(db)
    _insert(db, "mine", 31, 1, "org:1")
    _insert(db, "other", 3, 1, "org:1")
    db.commit()
    assert client.delete("/users/31", headers=owner).status_code == 200
    with Session(db.get_bind()) as fresh:
        assert fresh.execute(text("SELECT id FROM quiet_hours_deferred_deliveries")).scalars().all() == ["other"]


def test_user_deletion_without_the_table_is_unaffected(deletion_case):  # noqa: F811
    client, owner, db, _ = deletion_case
    assert client.delete("/users/31", headers=owner).status_code == 200


def test_org_deletion_removes_the_orgs_deferred_deliveries(review_db, monkeypatch):  # noqa: F811
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.organizations import routes
    review_db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(77,'Doomed org',1)"))
    review_db.commit()
    _apply(review_db)
    _insert(review_db, "owned", 3, 77, "system")           # org-owned, delivered by the system digest
    _insert(review_db, "scoped", None, None, "org:77", channel="webhook")
    _insert(review_db, "keep", 3, 1, "org:1")
    review_db.commit()
    app = FastAPI(); app.include_router(routes.router)

    def session():
        with Session(review_db.get_bind()) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {"id": 3, "username": "admin", "role": "admin", "group_id": None}
    with TestClient(app) as client:
        assert client.delete("/orgs/77").status_code == 200, "org delete failed"
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text("SELECT id FROM quiet_hours_deferred_deliveries")).scalars().all() == ["keep"]
