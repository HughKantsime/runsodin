"""Startup version policy without a server; the native proof is selected separately."""

import ast
from contextlib import contextmanager
import inspect
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event

from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
from core.schema.postgres_version import (
    FIRST_REFUSED_POSTGRES_MAJOR,
    TESTED_POSTGRES_MAJOR,
    check_postgres_version,
)
from tests.test_contracts.test_cert_E_notif_postgres import pg_engine  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]
SHOWS = ["SHOW server_version_num", "SHOW server_version"]


class FakeEngine:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, number=160016, version="16.16"):
        self.values = dict(zip(SHOWS, (str(number), version)))
        self.statements = []
        self.disposed = False

    @contextmanager
    def connect(self):
        yield self

    def exec_driver_sql(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalar_one=lambda: self.values[statement])

    def dispose(self):
        self.disposed = True


def refusal(major, version):
    return (
        f"ODIN does not support PostgreSQL {major} yet. "
        f"This server is PostgreSQL {version}. Nothing was changed. "
        "Use PostgreSQL 16, the version ODIN ships with "
        "(docker-compose.enterprise.yml provides one)."
    )


@pytest.mark.parametrize("number,version", [(180001, "18.1"), (190000, "19.0 (test build)")])
@pytest.mark.parametrize("entry", [check_postgres_version, bootstrap_database])
def test_refusal_reads_only_two_settings(number, version, entry):
    engine = FakeEngine(number, version)
    with pytest.raises(MigrationError) as caught:
        entry(engine)
    assert str(caught.value) == refusal(number // 10000, version)
    assert engine.statements == SHOWS


@pytest.mark.parametrize("number,version", [(170004, "17.4"), (150010, "15.10"), (140020, "14.20")])
def test_untested_versions_proceed_with_one_warning(number, version, caplog, monkeypatch):
    from core.schema import bootstrap
    from core import filament_evidence_backfill
    continued = Mock(return_value={"dialect": "postgresql"})
    monkeypatch.setattr(bootstrap, "_bootstrap_database_contract", continued)
    backfill = Mock()
    monkeypatch.setattr(filament_evidence_backfill, "backfill", backfill)
    engine = FakeEngine(number, version)
    with caplog.at_level(logging.WARNING):
        assert bootstrap_database(engine) == {"dialect": "postgresql"}
    continued.assert_called_once_with(engine, None)
    backfill.assert_called_once_with(engine)
    assert engine.statements == SHOWS
    assert caplog.messages == [f"ODIN is tested on PostgreSQL 16. This server is PostgreSQL {version}."]


def test_tested_version_proceeds_silently(caplog, monkeypatch):
    from core.schema import bootstrap
    from core import filament_evidence_backfill
    continued = Mock(return_value={"dialect": "postgresql"})
    monkeypatch.setattr(bootstrap, "_bootstrap_database_contract", continued)
    backfill = Mock()
    monkeypatch.setattr(filament_evidence_backfill, "backfill", backfill)
    engine = FakeEngine()
    with caplog.at_level(logging.WARNING):
        assert bootstrap_database(engine) == {"dialect": "postgresql"}
    continued.assert_called_once_with(engine, None)
    backfill.assert_called_once_with(engine)
    assert engine.statements == SHOWS
    assert caplog.messages == []


@pytest.mark.parametrize("major", [16, 18])
def test_version_check_precedes_schema_and_filament_backfill(monkeypatch, major):
    from core.schema import bootstrap
    from core import filament_evidence_backfill

    engine = FakeEngine(major * 10000, f"{major}.0")
    steps = []

    def schema(engine_arg, backend_root):
        assert engine_arg is engine and backend_root is None
        assert engine.statements == SHOWS
        steps.append("schema")
        return {"dialect": "postgresql"}

    def backfill(engine_arg):
        assert engine_arg is engine
        assert steps == ["schema"]
        steps.append("backfill")

    monkeypatch.setattr(bootstrap, "_bootstrap_database_contract", schema)
    monkeypatch.setattr(filament_evidence_backfill, "backfill", backfill)
    if major == 18:
        with pytest.raises(MigrationError, match="does not support PostgreSQL 18"):
            bootstrap_database(engine)
        assert steps == []
    else:
        assert bootstrap_database(engine) == {"dialect": "postgresql"}
        assert steps == ["schema", "backfill"]


def test_sqlite_bootstrap_never_calls_gate_or_show(monkeypatch):
    from core.schema import postgres_version
    gate = Mock(side_effect=AssertionError("PostgreSQL gate called for SQLite"))
    monkeypatch.setattr(postgres_version, "check_postgres_version", gate)
    engine = create_engine("sqlite:///:memory:")
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cursor, sql, params, context, many: statements.append(sql))
    try:
        assert bootstrap_database(engine)["dialect"] == "sqlite"
    finally:
        engine.dispose()
    gate.assert_not_called()
    assert not any(sql.lstrip().upper().startswith("SHOW ") for sql in statements)


@pytest.mark.parametrize("state,pending", [("staged", True), ("restore_in_progress", True), ("restore_in_progress", False)])
@pytest.mark.parametrize("refused_server", ["target", "maintenance"])
def test_restore_refuses_before_any_change_and_keeps_archive(tmp_path, monkeypatch, capsys, state, pending, refused_server):
    from modules.system import restore_coordinator as coordinator
    from tests.test_contracts.test_postgres_restore_boot_guard import _stage
    paths = _stage(tmp_path, state=state)
    if not pending:
        paths.pending.unlink()
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    engine = FakeEngine(180001, "18.1")
    target = FakeEngine() if refused_server == "maintenance" else engine
    factory = Mock(side_effect=[target, engine] if refused_server == "maintenance" else [engine])
    sweep = Mock(side_effect=AssertionError("cleanup must not run"))
    apply = Mock(side_effect=AssertionError("restore or frozen child must not run"))
    quarantine = Mock(side_effect=AssertionError("archive must not be quarantined"))
    sleep = Mock(side_effect=AssertionError("unsupported versions must not be retried"))
    monkeypatch.setattr(coordinator.time, "sleep", sleep)
    monkeypatch.setattr(coordinator, "create_database_engine", factory)
    monkeypatch.setattr(coordinator.restore_artifacts, "sweep_stale", sweep)
    monkeypatch.setattr(coordinator, "apply_pending_postgres_restore", apply)
    monkeypatch.setattr(coordinator, "quarantine_failed_pending_postgres_restore", quarantine)
    monkeypatch.setenv("ODIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_MAINTENANCE_URL", "postgresql://m@pg/postgres")
    monkeypatch.setattr(sys, "argv", ["restore_coordinator", "--database-url", "postgresql://o@pg/odin"])
    assert coordinator.main() == 1
    output = capsys.readouterr()
    assert output.err == refusal(18, "18.1") + "\n" and output.out == ""
    assert engine.statements == SHOWS and engine.disposed
    assert factory.call_count == (2 if refused_server == "maintenance" else 1)
    assert all(call.kwargs["role"] == "restore" for call in factory.call_args_list)
    assert target.statements == SHOWS and target.disposed
    sweep.assert_not_called()
    apply.assert_not_called()
    quarantine.assert_not_called()
    sleep.assert_not_called()
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert paths.pending.exists() == pending and paths.manifest.exists()


@pytest.mark.parametrize("error", [None, ConnectionError("not ready"), TimeoutError("timed out"), PermissionError("unreadable credentials"), ValueError("invalid configuration")])
def test_no_restore_version_probe_is_best_effort(tmp_path, monkeypatch, capsys, error):
    from modules.system import restore_coordinator as coordinator
    engine = FakeEngine()
    factory = Mock(side_effect=error, return_value=engine)
    sweep = Mock(return_value={"databases": [], "files": []})
    sleep = Mock()
    quarantine = Mock()
    monkeypatch.setattr(coordinator, "create_database_engine", factory)
    monkeypatch.setattr(coordinator.restore_artifacts, "sweep_stale", sweep)
    monkeypatch.setattr(coordinator.time, "sleep", sleep)
    monkeypatch.setattr(coordinator, "quarantine_failed_pending_postgres_restore", quarantine)
    monkeypatch.setenv("ODIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_MAINTENANCE_URL", "postgresql://m@pg/postgres")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", "/unused-app")
    monkeypatch.setenv("DATABASE_MAINTENANCE_PASSWORD_FILE", "/unused-maintenance")
    monkeypatch.setattr(sys, "argv", ["restore_coordinator", "--database-url", "postgresql://o@pg/odin"])
    assert coordinator.main() == 0
    factory.assert_called_once()
    sweep.assert_called_once()
    sleep.assert_not_called()
    quarantine.assert_not_called()
    assert capsys.readouterr().err == ""


def test_no_restore_still_refuses_a_shared_maintenance_secret(tmp_path, monkeypatch, capsys):
    """The apply step's environment checks ran on every start before the version gate; they still do."""
    from modules.system import restore_coordinator as coordinator
    from modules.system.postgres_backup_service import RestoreEnvironmentError
    secret = tmp_path / "secret"
    secret.write_text("x")
    monkeypatch.setattr(coordinator, "create_database_engine", Mock(return_value=FakeEngine()))
    monkeypatch.setattr(coordinator.restore_artifacts, "sweep_stale", Mock(return_value={"databases": [], "files": []}))
    sleep = Mock()
    monkeypatch.setattr(coordinator.time, "sleep", sleep)
    monkeypatch.setenv("ODIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_MAINTENANCE_URL", "postgresql://m@pg/postgres")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("DATABASE_MAINTENANCE_PASSWORD_FILE", str(secret))
    monkeypatch.setattr(sys, "argv", ["restore_coordinator", "--database-url", "postgresql://o@pg/odin"])
    with pytest.raises(RestoreEnvironmentError, match="must be distinct"):
        coordinator.main()
    sleep.assert_not_called()


def test_no_restore_unsupported_version_stops_before_sweep(tmp_path, monkeypatch, capsys):
    from modules.system import restore_coordinator as coordinator
    engine = FakeEngine(180001, "18.1")
    factory = Mock(return_value=engine)
    sweep, apply, quarantine, sleep = Mock(), Mock(), Mock(), Mock()
    monkeypatch.setattr(coordinator, "create_database_engine", factory)
    monkeypatch.setattr(coordinator.restore_artifacts, "sweep_stale", sweep)
    monkeypatch.setattr(coordinator, "apply_pending_postgres_restore", apply)
    monkeypatch.setattr(coordinator, "quarantine_failed_pending_postgres_restore", quarantine)
    monkeypatch.setattr(coordinator.time, "sleep", sleep)
    monkeypatch.setenv("ODIN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_MAINTENANCE_URL", "postgresql://m@pg/postgres")
    monkeypatch.setattr(sys, "argv", ["restore_coordinator", "--database-url", "postgresql://o@pg/odin"])
    assert coordinator.main() == 1
    assert capsys.readouterr().err == refusal(18, "18.1") + "\n"
    factory.assert_called_once()
    assert engine.statements == SHOWS and engine.disposed
    for operation in (sweep, apply, quarantine, sleep):
        operation.assert_not_called()


def test_restore_preflight_retries_connection_then_checks_once(tmp_path, monkeypatch, caplog):
    from modules.system import restore_coordinator as coordinator
    engine = FakeEngine(170004, "17.4")
    factory = Mock(side_effect=[ConnectionError("not ready"), engine])
    apply = Mock(side_effect=[ConnectionError("try again"), None])
    sweep = Mock(return_value={"databases": [], "files": []})
    sleep = Mock()
    monkeypatch.setenv("ODIN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(coordinator, "create_database_engine", factory)
    monkeypatch.setattr(coordinator, "apply_pending_postgres_restore", apply)
    monkeypatch.setattr(coordinator.restore_artifacts, "sweep_stale", sweep)
    monkeypatch.setattr(coordinator.time, "sleep", sleep)
    with caplog.at_level(logging.WARNING):
        assert coordinator._apply_postgres_with_retry("postgresql://o@pg/odin", "postgresql://m@pg/postgres", None, None) is None
    assert factory.call_count == 2 and apply.call_count == 2
    assert engine.statements == SHOWS and engine.disposed
    sweep.assert_called_once()
    assert [call.args for call in sleep.call_args_list] == [(2,), (4,)]
    assert caplog.messages == []  # The following bootstrap owns the startup warning.


def test_bootstrap_script_prints_plain_refusal_and_exits_nonzero():
    code = (
        "from contextlib import contextmanager\nfrom types import SimpleNamespace\n"
        f"SHOWS = {SHOWS!r}\n" + inspect.getsource(FakeEngine) + """
from scripts import bootstrap_database as script
engine = FakeEngine(180001, '18.1')
script.create_database_engine = lambda *a, **k: engine
code = script.main()
assert engine.disposed
assert engine.statements == ['SHOW server_version_num', 'SHOW server_version']
raise SystemExit(code)
"""
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", code], cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend"), "DATABASE_URL": "postgresql://o@pg/odin"},
        timeout=60,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == refusal(18, "18.1") + "\n"
    assert "Traceback" not in result.stderr



def test_policy_literals_match_readme():
    source = (ROOT / "backend/core/schema/postgres_version.py").read_text()
    values = {
        target.id: node.value.value
        for node in ast.parse(source).body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets if isinstance(target, ast.Name)
    }
    assert values["TESTED_POSTGRES_MAJOR"] == TESTED_POSTGRES_MAJOR == 16
    assert values["FIRST_REFUSED_POSTGRES_MAJOR"] == FIRST_REFUSED_POSTGRES_MAJOR == 18
    assert (
        f"ODIN ships with and is tested on PostgreSQL {TESTED_POSTGRES_MAJOR}; "
        f"PostgreSQL {FIRST_REFUSED_POSTGRES_MAJOR} is not supported yet, and ODIN stops at start-up "
        "on it without changing anything."
    ) in (ROOT / "README.md").read_text()


def test_every_pinned_postgres_image_records_tested_major():
    pattern = r"postgres(:[^@[:space:]]+)?@sha256:[0-9a-f]{64}"
    result = subprocess.run(
        ["git", "grep", "-n", "-I", "-E", pattern, "--", ":(exclude)*.env*"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    digests = set()
    for line in result.stdout.splitlines():
        path, number, source = line.split(":", 2)
        recorded = re.search(r"# PostgreSQL (\d+)\.16 \(Alpine\); read from the image by Dev on 2026-11-20\.", source)
        assert recorded and int(recorded[1]) == TESTED_POSTGRES_MAJOR, f"{path}:{number} lacks the tested major"
        digests.update(re.findall(r"sha256:([0-9a-f]{64})", source))
    assert digests == {"1d5b02d3a9941fbd52437db4107f8c52d6297dac553ca1e5c299ebb3756b4857"}


@pytest.mark.postgresql
def test_real_postgres_16_bootstrap_checks_version_once(pg_engine):
    assert pg_engine.dialect.server_version_info[0] == TESTED_POSTGRES_MAJOR
    statements = []

    def record(connection, cursor, sql, params, context, many):
        statements.append(sql)

    event.listen(pg_engine, "before_cursor_execute", record)
    try:
        result = bootstrap_database(pg_engine)
    finally:
        event.remove(pg_engine, "before_cursor_execute", record)
    assert result["dialect"] == "postgresql" and result["applied"] == []
    assert [sql for sql in statements if sql.startswith("SHOW server_version")] == SHOWS
    assert statements[:2] == SHOWS
