"""Restore staging stays off the event loop after controlled-access checks."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from core.schema import bootstrap_database


@pytest.fixture
def unmarked_db(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'unmarked.db'))
    bootstrap_database(engine)
    try:
        with Session(engine) as db:
            yield db
    finally:
        engine.dispose()


# --- A restore upload must not freeze the API while a backup holds the lock.

def test_staging_a_restore_runs_off_the_event_loop(unmarked_db, monkeypatch):
    import asyncio
    from modules.system import routes_backup
    seen = []
    def stage(path, url, *, actor_id):
        try:
            asyncio.get_running_loop()
            seen.append("on the event loop")
        except RuntimeError:
            seen.append("in a worker thread")
        return {"pre_restore_backup": "x", "candidate_sha256": "y"}
    class Upload:
        filename = "candidate.db"
        chunks = [b"SQLite format 3\x00", b""]
        async def read(self, _):
            return self.chunks.pop(0)
    monkeypatch.setattr(routes_backup, "stage_restore", stage)
    result = asyncio.run(routes_backup.restore_backup(file=Upload(), current_user={"id": 1}, db=unmarked_db))
    assert result["status"] == "staged" and seen == ["in a worker thread"]


def test_staging_a_postgresql_restore_runs_off_the_event_loop(unmarked_db, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from modules.system import routes_backup
    seen = []
    def stage(path, url, maintenance, **kwargs):
        try:
            asyncio.get_running_loop()
            seen.append("on the event loop")
        except RuntimeError:
            seen.append("in a worker thread")
        seen.append((maintenance, kwargs["actor_id"], kwargs["password_file"], kwargs["maintenance_password_file"]))
        return {"pre_restore_backup": None, "candidate_sha256": "y"}
    class Upload:
        filename = "candidate.dump"
        chunks = [b"PGDMP", b""]
        async def read(self, _):
            return self.chunks.pop(0)
    monkeypatch.setattr(routes_backup, "stage_postgres_restore", stage)
    monkeypatch.setattr(routes_backup, "sql", SimpleNamespace(is_postgres=True))
    monkeypatch.setattr(routes_backup, "_postgres_maintenance_config", lambda: ("maintenance-url", "app-file", "maintenance-file"))
    result = asyncio.run(routes_backup.restore_backup(file=Upload(), current_user={"id": 7}, db=unmarked_db))
    assert result["status"] == "staged"
    assert seen == ["in a worker thread", ("maintenance-url", 7, "app-file", "maintenance-file")]


