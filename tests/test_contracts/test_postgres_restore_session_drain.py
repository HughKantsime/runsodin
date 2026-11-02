"""The restore's session check waits out a closing connection, and still refuses a real one."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="SQLAlchemy not installed in test venv")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from modules.system import postgres_backup_service as service  # noqa: E402
from modules.system.backup_service import BackupValidationError  # noqa: E402


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Connection:
    """pg_stat_activity as a list of reads, which keeps repeating its last one."""

    def __init__(self, *reads):
        self.reads = list(reads)
        self.statements = []

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.statements.append(sql)
        if "pg_stat_clear_snapshot" in sql:
            return _Result([])
        assert parameters == {"database_name": "odin"}
        assert "pg_backend_pid()" in sql
        return _Result(self.reads.pop(0) if len(self.reads) > 1 else self.reads[0])


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(service, "TARGET_SESSION_DRAIN_POLL_SECONDS", 0)
    monkeypatch.setattr(service, "TARGET_SESSION_DRAIN_SECONDS", 0.05)


def test_no_sessions_returns_on_the_first_read(monkeypatch):
    monkeypatch.setattr(service.time, "sleep", lambda seconds: pytest.fail("must not wait"))
    connection = _Connection([])
    service._assert_no_target_sessions(connection, "odin")
    assert len(connection.statements) == 2


def test_a_closing_session_is_waited_out_on_fresh_snapshots(fast):
    connection = _Connection([(10, "client backend")], [(10, "client backend")], [])
    service._assert_no_target_sessions(connection, "odin")
    reads = [sql for sql in connection.statements if "pg_stat_activity" in sql]
    clears = [sql for sql in connection.statements if "pg_stat_clear_snapshot" in sql]
    assert len(reads) == 3
    assert len(clears) == 3
    assert "pg_stat_clear_snapshot" in connection.statements[0]


def test_a_session_that_stays_is_refused_and_its_type_logged(fast, caplog):
    connection = _Connection([(10, "client backend"), (11, "autovacuum worker")])
    with pytest.raises(BackupValidationError, match="while 2 target database session"):
        service._assert_no_target_sessions(connection, "odin")
    assert "autovacuum worker" in caplog.text


def test_excluded_pids_never_count_and_never_wait(monkeypatch):
    monkeypatch.setattr(service.time, "sleep", lambda seconds: pytest.fail("must not wait"))
    connection = _Connection([(10, "client backend")])
    service._assert_no_target_sessions(connection, "odin", excluded_pids=frozenset({10}))


def test_the_wait_is_bounded_for_real():
    assert 0 < service.TARGET_SESSION_DRAIN_SECONDS <= 30
    assert 0 < service.TARGET_SESSION_DRAIN_POLL_SECONDS < 1
