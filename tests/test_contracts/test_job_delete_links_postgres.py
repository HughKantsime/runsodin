"""Delete linked jobs on PostgreSQL with both pre-cascade constraints."""
import pytest

from tests.test_contracts.test_cert_E_notif_postgres import pg_engine  # noqa: F401
from tests.test_contracts.test_job_delete_links import assert_links, client_for, delete_via


@pytest.mark.postgresql
@pytest.mark.parametrize("path", ["single", "bulk", "orgs", "retention"])
def test_legacy_postgres_job_deletes(pg_engine, monkeypatch, enterprise_license, path):
    # The outer transaction restores the module fixture's constraints and data.
    with pg_engine.connect() as db:
        db.begin()
        for table in ("alerts", "spool_usage"):
            db.exec_driver_sql(f"ALTER TABLE {table} DROP CONSTRAINT {table}_job_id_fkey, "
                               f"ADD FOREIGN KEY (job_id) REFERENCES jobs(id)")
        with client_for(db, monkeypatch) as client:
            response = delete_via(client, path)
            assert response.status_code == (204 if path == "single" else 200), response.text
        assert_links(db, deleted=True)
