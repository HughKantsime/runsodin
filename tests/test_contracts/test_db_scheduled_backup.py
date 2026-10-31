"""Daily verified backups: a damaged database, or any failure, must not push out a good backup."""
import json
import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.db_integrity import DAMAGED, IntegrityReport, UNAVAILABLE, check_database
from modules.system import backup_failure_alerts, backup_schedule, backup_service
from modules.system.backup_schedule import (
    KEEP_DAILY,
    KEEP_WEEKLY,
    PARTIAL_PREFIX,
    RECEIPT_SUFFIX,
    create_scheduled_backup,
    prune_scheduled_backups,
    retained_backups,
)
from modules.system.backup_service import (
    BackupValidationError,
    SourceDatabaseError,
    create_online_backup,
    stage_restore,
    validate_database,
)
from tests.test_contracts.db_damage_support import overwrite_rightmost_leaf, rewind_rightmost_leaf

NOW = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    from core.schema import bootstrap_database
    database = tmp_path_factory.mktemp("template") / "odin.db"
    engine = create_engine(f"sqlite:///{database}")
    bootstrap_database(engine)
    with engine.begin() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        connection.execute(text("INSERT INTO groups(id, name, is_org) VALUES(1, 'org', 1)"))
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        connection.execute(text("INSERT INTO printers(id, name, org_id) VALUES(1, 'one', 1)"))
        for number in range(3000):
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
            connection.execute(text("INSERT INTO audit_logs(action, entity_type) VALUES(:a, 'x')"), {"a": f"a{number}"})
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
            connection.execute(text("INSERT INTO printer_telemetry(printer_id, recorded_at) VALUES(1, '2026-10-30 00:00:00')"))
    engine.dispose()
    return database


@pytest.fixture
def database(template, tmp_path):
    copied = tmp_path / "odin.db"
    copied.write_bytes(template.read_bytes())
    return copied


@pytest.fixture
def alerts(monkeypatch):
    """Records alerts. The restore test has its own tests below, and it starts a process per backup."""
    seen = []
    monkeypatch.setattr(backup_failure_alerts, "publish_backup_failure", seen.append)
    monkeypatch.setattr(backup_schedule, "rehearse_restore", lambda *_: None)
    return seen


def _url(database: Path) -> str:
    return f"sqlite:///{database}"


def _odin_backups(database: Path) -> list[str]:
    return sorted(path.name for path in (database.parent / "backups").glob("odin_backup_*.db"))


def test_scheduled_backup_is_verified_private_and_has_a_receipt(database, alerts):
    result = create_scheduled_backup(_url(database), now=NOW)
    backup = database.parent / "backups" / result["filename"]
    assert _odin_backups(database) == [result["filename"]]
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert validate_database(backup)["sha256"] == result["sha256"]
    receipt = json.loads(backup.with_name(backup.name + RECEIPT_SUFFIX).read_text())
    assert receipt == {"schema_version": 1, "created_at": NOW.isoformat(), "source_integrity": "ok",
                       "sha256": result["sha256"], "size_bytes": result["size_bytes"]}
    assert result["source_report"].state == "ok"
    assert alerts == []
    assert not list(database.parent.joinpath("backups").glob(f"{PARTIAL_PREFIX}*"))


def _rewind(database: Path) -> None:
    rewind_rightmost_leaf(database, "printer_telemetry", "UPDATE printer_telemetry SET recorded_at = '2026-11-02 00:00:00'")


def _damage_vacuum_cannot_see(database: Path) -> None:
    """The incident's shape, where the wrong rows fit: retention already removed the ids they reuse."""
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM audit_logs WHERE id <= 200")
    printers = connection.execute("PRAGMA table_info(printers)").fetchall()
    for position in (row[0] for row in connection.execute("PRAGMA table_info(audit_logs)") if row[3] and not row[5]):
        # The column name comes from this database's own schema.
        connection.execute(f"UPDATE printers SET \"{printers[position][1]}\" = 'x'")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query
    connection.commit()
    connection.close()
    overwrite_rightmost_leaf(database, "audit_logs", "printers")


def test_vacuum_into_can_hide_damage_so_the_source_has_to_be_checked(database):
    """Why require_healthy_source exists: the source fails its check, and the copy validates."""
    _damage_vacuum_cannot_see(database)
    assert check_database(database).state == DAMAGED
    target = database.parent / "copy.db"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.execute("VACUUM INTO ?", (str(target),))
    connection.close()
    assert validate_database(target)["size_bytes"] > 0
    assert check_database(target).state == "ok"


def test_vacuum_into_copy_of_a_stale_page_fails_validation_by_itself(database):
    """It copies indexes as they are, so this kind carries over and validate_database catches it."""
    _rewind(database)
    target = database.parent / "copy.db"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.execute("VACUUM INTO ?", (str(target),))
    connection.close()
    with pytest.raises(BackupValidationError):
        validate_database(target)


def test_vacuum_into_refuses_the_incident_shape_with_a_rowid_collision(database):
    """Production's damage would have failed loudly here; a stale page wouldn't. Both are covered."""
    overwrite_rightmost_leaf(database, "audit_logs", "printers")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("VACUUM INTO ?", (str(database.parent / "copy.db"),))
    connection.close()


@pytest.mark.parametrize("damage", [
    lambda database: overwrite_rightmost_leaf(database, "audit_logs", "printers"),
    _rewind,
    _damage_vacuum_cannot_see,
], ids=["incident", "stale-page", "hidden-from-vacuum"])
def test_damaged_source_makes_no_backup_and_keeps_the_last_good_ones(database, alerts, damage):
    good = [create_scheduled_backup(_url(database), now=NOW - timedelta(days=day))["filename"]
            for day in range(40, 0, -5)]
    kept = _odin_backups(database)
    assert set(kept) <= set(good) and kept
    damage(database)
    with pytest.raises(SourceDatabaseError) as caught:
        create_scheduled_backup(_url(database), now=NOW)
    assert caught.value.report.state == DAMAGED
    assert _odin_backups(database) == kept
    assert [backup_failure_alerts.backup_failure_reason(error)[0] for error in alerts] == ["source_integrity"] * len(alerts) and alerts


def test_source_that_cannot_be_checked_is_refused_and_says_so(database, alerts, monkeypatch):
    monkeypatch.setattr(backup_service, "check_database", lambda _: IntegrityReport(UNAVAILABLE, 0.0))
    with pytest.raises(SourceDatabaseError) as caught:
        create_scheduled_backup(_url(database), now=NOW)
    assert _odin_backups(database) == []
    reason, message = backup_failure_alerts.backup_failure_reason(caught.value)
    assert reason == "source_unavailable" and "could not be checked" in message and alerts


@pytest.mark.parametrize("failure", ["snapshot", "disk", "verify", "publish"])
def test_a_failed_run_publishes_nothing_prunes_nothing_and_alerts(database, alerts, monkeypatch, failure):
    days = KEEP_DAILY + KEEP_WEEKLY * 7 + 3
    for day in range(days, 0, -7):
        create_scheduled_backup(_url(database), now=NOW - timedelta(days=day))
    before = _odin_backups(database)
    # Old enough that a successful run would prune one of them.
    extra = before[0]
    if failure == "snapshot":
        monkeypatch.setattr(backup_service, "validate_database", lambda _: (_ for _ in ()).throw(BackupValidationError("snapshot failed validation")))
    elif failure == "disk":
        monkeypatch.setattr(backup_service.shutil, "disk_usage", lambda _: type("U", (), {"free": 1})())
    elif failure == "verify":
        monkeypatch.setattr(backup_schedule, "verify_backup", lambda *_: (_ for _ in ()).throw(BackupValidationError("verification failed")))
    else:
        original = backup_schedule.os.replace
        def fail_publish(source, target):
            if Path(target).name.startswith("odin_backup_") and str(target).endswith(".db"):
                raise OSError(5, "synthetic")
            return original(source, target)
        monkeypatch.setattr(backup_schedule.os, "replace", fail_publish)
    with pytest.raises((BackupValidationError, OSError)):
        create_scheduled_backup(_url(database), now=NOW)
    assert _odin_backups(database) == before and extra in before
    assert not list((database.parent / "backups").glob(f"{PARTIAL_PREFIX}*"))
    assert alerts


def test_a_receipt_is_only_written_after_verification_passes(database, alerts, monkeypatch):
    monkeypatch.setattr(backup_schedule, "verify_backup", lambda *_: (_ for _ in ()).throw(BackupValidationError("verification failed")))
    with pytest.raises(BackupValidationError):
        create_scheduled_backup(_url(database), now=NOW)
    assert not list((database.parent / "backups").glob(f"*{RECEIPT_SUFFIX}"))


def _scheduled(directory: Path, when: datetime) -> Path:
    backup = directory / f"odin_backup_{when:%Y%m%d_%H%M%S}_000000.db"
    backup.write_bytes(b"x")
    backup.with_name(backup.name + RECEIPT_SUFFIX).write_text(json.dumps({"created_at": when.isoformat()}))
    return backup


def test_retention_keeps_seven_days_and_four_weeks_and_nothing_it_did_not_make(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir()
    made = [_scheduled(directory, NOW - timedelta(days=day)) for day in range(60)]
    # Files it must never touch, however old: no receipt, or not the scheduled name.
    manual = directory / "odin_backup_20200101_000000_000000.db"
    adhoc = directory / "odin-pre-repair-20200101.db"
    bad_receipt = directory / "odin_backup_20200102_000000_000000.db"
    for path in (manual, adhoc, bad_receipt):
        path.write_bytes(b"x")
    bad_receipt.with_name(bad_receipt.name + RECEIPT_SUFFIX).write_text("{not json")
    removed = prune_scheduled_backups(directory)
    left = {path for path in made if path.exists()}
    newest = {NOW - timedelta(days=day) for day in range(KEEP_DAILY)}
    assert {_scheduled_time(path) for path in left} >= newest
    assert len(left) <= KEEP_DAILY + KEEP_WEEKLY and len(left) >= KEEP_DAILY
    weeks = {_scheduled_time(path).isocalendar()[:2] for path in left}
    assert len(weeks) >= KEEP_WEEKLY
    assert len(removed) == len(made) - len(left)
    assert manual.exists() and adhoc.exists() and bad_receipt.exists()
    assert not any(path.with_name(path.name + RECEIPT_SUFFIX).exists() for path in made if path not in left)


def _scheduled_time(path: Path) -> datetime:
    return datetime.strptime(path.name[len("odin_backup_"):-len("_000000.db")], "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


def test_two_runs_in_a_day_count_as_one_day(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    retained = retained_backups([(NOW, a), (NOW - timedelta(hours=1), b)])
    assert retained == {a}


def test_a_new_backup_in_an_old_directory_prunes_only_after_it_is_good(database, alerts):
    directory = database.parent / "backups"
    directory.mkdir()
    old = [_scheduled(directory, NOW - timedelta(days=day)) for day in range(100, 170)]
    result = create_scheduled_backup(_url(database), now=NOW)
    assert result["pruned"] and (directory / result["filename"]).exists()
    assert sum(path.exists() for path in old) < len(old)


def test_a_failed_prune_does_not_fail_the_good_backup(database, alerts, monkeypatch):
    monkeypatch.setattr(backup_schedule, "prune_scheduled_backups", lambda _: (_ for _ in ()).throw(OSError(13, "synthetic")))
    result = create_scheduled_backup(_url(database), now=NOW)
    assert result["pruned"] == [] and _odin_backups(database) == [result["filename"]]


def test_deleted_backup_loses_its_receipt_and_no_file_is_recreated(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir()
    backup = _scheduled(directory, NOW)
    backup.unlink()
    assert prune_scheduled_backups(directory) == []
    assert list(directory.iterdir()) == []


def test_a_killed_run_leaves_nothing_listed_and_the_next_run_clears_it(database, alerts):
    directory = database.parent / "backups"
    directory.mkdir()
    leftover = directory / f"{PARTIAL_PREFIX}20261101_000000_000000.db"
    leftover.write_bytes(b"half written")
    assert _odin_backups(database) == []
    result = create_scheduled_backup(_url(database), now=NOW)
    assert not leftover.exists() and _odin_backups(database) == [result["filename"]]


def test_manual_backup_of_a_damaged_database_is_refused_with_the_reason(database, alerts):
    _damage_vacuum_cannot_see(database)
    with pytest.raises(SourceDatabaseError):
        create_online_backup(_url(database))
    assert _odin_backups(database) == []
    assert backup_failure_alerts.backup_failure_reason(alerts[0])[0] == "source_integrity"


def test_a_good_backup_can_still_be_staged_over_a_damaged_database(database, alerts, tmp_path):
    """The source check isn't on this path: a restore is how you get away from the damage."""
    good = create_scheduled_backup(_url(database), now=NOW)
    candidate = tmp_path / "candidate.db"
    candidate.write_bytes((database.parent / "backups" / good["filename"]).read_bytes())
    _damage_vacuum_cannot_see(database)
    staged = stage_restore(candidate, _url(database))
    assert staged["restart_required"] is True and staged["pre_restore_backup"].startswith("pre_restore_")


def test_staging_stops_cleanly_when_the_damage_also_breaks_its_safety_copy(database, alerts, tmp_path):
    """The limit the runbook warns about: the safety copy is VACUUM INTO, which can fail on damage."""
    good = create_scheduled_backup(_url(database), now=NOW)
    candidate = tmp_path / "candidate.db"
    candidate.write_bytes((database.parent / "backups" / good["filename"]).read_bytes())
    overwrite_rightmost_leaf(database, "audit_logs", "printers")
    with pytest.raises(BackupValidationError):
        stage_restore(candidate, _url(database))
    assert not (database.parent / "restore-pending.db").exists()
    assert not (database.parent / "restore-pending.json").exists()


def test_failure_reason_for_source_damage_has_no_path_or_exception_text(database):
    overwrite_rightmost_leaf(database, "audit_logs", "printers")
    error = SourceDatabaseError(check_database(database))
    reason, message = backup_failure_alerts.backup_failure_reason(error)
    assert reason == "source_integrity" and "Existing backups were kept" in message
    assert str(database) not in message and "Tree" not in message


def test_source_damage_alert_is_saved_once_per_day(tmp_path, monkeypatch):
    from core.schema import bootstrap_database
    engine = create_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    bootstrap_database(engine)
    with engine.begin() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        connection.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'admin','x','admin',1,NULL),(2,'viewer','x','viewer',1,NULL)"))
    factory = sessionmaker(bind=engine)
    error = SourceDatabaseError(IntegrityReport(DAMAGED, 0.0, 1))
    for _ in range(2):
        backup_failure_alerts.publish_backup_failure(error, session_factory=factory, now=NOW)
    with engine.connect() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        rows = connection.execute(text("SELECT user_id, severity, title, message FROM alerts")).all()
    assert len(rows) == 1 and rows[0][0] == 1 and str(rows[0][1]).lower() == "critical"
    assert "integrity check" in rows[0][3]
    engine.dispose()


def test_scheduled_backup_never_takes_the_restore_pending_path(database, alerts):
    create_scheduled_backup(_url(database), now=NOW)
    assert not (database.parent / "restore-pending.db").exists()
    assert os.path.exists(database.parent / ".odin-restore.lock")


def _digest(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_restore_test_boots_a_copy_and_never_touches_live_data_or_the_backup(database, monkeypatch):
    monkeypatch.setattr(backup_failure_alerts, "publish_backup_failure", lambda _: None)
    result = create_scheduled_backup(_url(database), now=NOW)
    backup = database.parent / "backups" / result["filename"]
    live, copied = _digest(database), _digest(backup)
    calls = []
    original = backup_schedule.subprocess.run
    monkeypatch.setattr(backup_schedule.subprocess, "run", lambda *a, **k: calls.append(k["env"]["DATABASE_URL"]) or original(*a, **k))
    backup_schedule.rehearse_restore(backup, database)
    assert len(calls) == 1 and str(database) not in calls[0].replace("backups", "") and calls[0].endswith("/odin.db")
    assert "partial_scheduled_" in calls[0]
    assert _digest(database) == live and _digest(backup) == copied
    assert [path.name for path in backup.parent.iterdir() if path.name.startswith(PARTIAL_PREFIX)] == []


def test_restore_test_refuses_a_damaged_backup(database, tmp_path):
    bad = database.parent / "bad.db"
    bad.write_bytes(database.read_bytes())
    overwrite_rightmost_leaf(bad, "audit_logs", "printers")
    with pytest.raises(BackupValidationError, match="restore test"):
        backup_schedule.rehearse_restore(bad, database)
    assert not list(database.parent.glob(f"{PARTIAL_PREFIX}*"))


def test_restore_test_refuses_a_schema_that_differs_from_live(database, monkeypatch):
    monkeypatch.setattr(backup_schedule, "_live_fingerprint", lambda _: "not the same schema")
    copy = database.parent / "copy.db"
    copy.write_bytes(database.read_bytes())
    with pytest.raises(BackupValidationError, match="schema differs"):
        backup_schedule.rehearse_restore(copy, database)


def test_live_fingerprint_matches_what_startup_reports(database):
    from core.schema import schema_fingerprint
    engine = create_engine(_url(database))
    with engine.connect() as connection:
        expected = schema_fingerprint(connection)
    engine.dispose()
    assert backup_schedule._live_fingerprint(database) == expected


def test_restore_test_needing_migrations_or_printing_nothing_fails(database, monkeypatch):
    class Done:
        returncode = 0
        stdout = '{"applied": ["python:999"], "schema_fingerprint": "x"}\n'
    monkeypatch.setattr(backup_schedule.subprocess, "run", lambda *a, **k: Done)
    with pytest.raises(BackupValidationError, match="needs migrations"):
        backup_schedule.rehearse_restore(database, database)
    Done.stdout = ""
    with pytest.raises(BackupValidationError, match="no startup result"):
        backup_schedule.rehearse_restore(database, database)


def test_a_failed_restore_test_publishes_nothing_prunes_nothing_and_alerts(database, alerts, monkeypatch):
    first = create_scheduled_backup(_url(database), now=NOW - timedelta(days=1))
    monkeypatch.setattr(backup_schedule, "rehearse_restore", lambda *_: (_ for _ in ()).throw(BackupValidationError("Backup failed its restore test: startup refused it")))
    with pytest.raises(BackupValidationError):
        create_scheduled_backup(_url(database), now=NOW)
    assert _odin_backups(database) == [first["filename"]]
    assert backup_failure_alerts.backup_failure_reason(alerts[-1])[0] == "restore_test"
    assert not list((database.parent / "backups").glob(f"*{RECEIPT_SUFFIX}.tmp"))


def test_killed_restore_test_directory_is_swept(database, alerts):
    leftover = database.parent / "backups" / f"{PARTIAL_PREFIX}restore"
    leftover.mkdir(parents=True)
    (leftover / "odin.db").write_bytes(b"plaintext copy")
    create_scheduled_backup(_url(database), now=NOW)
    assert not leftover.exists()
