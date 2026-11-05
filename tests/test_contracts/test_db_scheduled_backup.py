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
from tests.test_contracts.db_damage_support import break_freelist, overwrite_rightmost_leaf, rewind_rightmost_leaf

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
    assert stat.S_IMODE(backup.with_name(backup.name + RECEIPT_SUFFIX).stat().st_mode) == 0o600
    receipt = json.loads(backup.with_name(backup.name + RECEIPT_SUFFIX).read_text())
    assert receipt == {"schema_version": 1, "created_at": NOW.isoformat(), "source_integrity": "ok",
                       "sha256": result["sha256"], "size_bytes": result["size_bytes"]}
    assert result["source_report"].state == "ok"
    assert alerts == []
    assert not list(database.parent.joinpath("backups").glob(f"{PARTIAL_PREFIX}*"))


def _rewind(database: Path) -> None:
    rewind_rightmost_leaf(database, "printer_telemetry", "UPDATE printer_telemetry SET recorded_at = '2026-11-02 00:00:00'")


def _damage_vacuum_cannot_see(database: Path) -> None:
    break_freelist(database, "printer_telemetry")


def _vacuum_copy_validates(database: Path) -> bool:
    target = database.parent / "copy.db"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("VACUUM INTO ?", (str(target),))
        validate_database(target)
    except (sqlite3.Error, BackupValidationError):
        return False
    finally:
        connection.close()
        target.unlink(missing_ok=True)
    return True


def test_vacuum_into_can_hide_damage_so_the_source_has_to_be_checked(database):
    """Why require_healthy_source exists: the source fails its check, and the copy validates."""
    _damage_vacuum_cannot_see(database)
    assert check_database(database).state == DAMAGED
    assert _vacuum_copy_validates(database) is True


@pytest.mark.parametrize("damage", [
    lambda database: overwrite_rightmost_leaf(database, "audit_logs", "printers"),
    _rewind,
], ids=["incident", "stale-page"])
def test_vacuum_into_carries_or_refuses_this_damage(database, damage):
    """These don't validate, by a constraint error or in the copy, depending on the SQLite version."""
    damage(database)
    assert check_database(database).state == DAMAGED
    assert _vacuum_copy_validates(database) is False


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
    # Enough old copies that any prune would remove some of them.
    directory = database.parent / "backups"
    directory.mkdir()
    for day in range(20, 90):
        _scheduled(directory, NOW - timedelta(days=day))
    before = _odin_backups(database)
    if failure == "snapshot":
        monkeypatch.setattr(backup_service, "validate_database", lambda _: (_ for _ in ()).throw(BackupValidationError("snapshot failed validation")))
    elif failure == "disk":
        monkeypatch.setattr(backup_service.shutil, "disk_usage", lambda _: type("U", (), {"free": 1})())
    elif failure == "verify":
        monkeypatch.setattr(backup_schedule, "verify_sqlite_file", lambda *_: (_ for _ in ()).throw(BackupValidationError("verification failed")))
    else:
        original = backup_schedule.os.replace
        def fail_publish(source, target):
            if Path(target).name.startswith("odin_backup_") and str(target).endswith(".db"):
                raise OSError(5, "synthetic")
            return original(source, target)
        monkeypatch.setattr(backup_schedule.os, "replace", fail_publish)
    with pytest.raises((BackupValidationError, OSError)):
        create_scheduled_backup(_url(database), now=NOW)
    assert _odin_backups(database) == before and len(before) == 70
    assert not list(directory.glob(f"{PARTIAL_PREFIX}*"))
    assert alerts


def test_a_receipt_is_only_written_after_verification_passes(database, alerts, monkeypatch):
    monkeypatch.setattr(backup_schedule, "verify_sqlite_file", lambda *_: (_ for _ in ()).throw(BackupValidationError("verification failed")))
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
    newest = {NOW - timedelta(days=day) for day in range(7)}
    assert {_scheduled_time(path) for path in left} >= newest
    assert 7 <= len(left) <= 11
    weeks = {_scheduled_time(path).isocalendar()[:2] for path in left}
    assert len(weeks) >= 4
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
    with pytest.raises(BackupValidationError, match="startup refused"):
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


# --- Free space: a run holds two full copies at its peak and must leave room for live writes.

def _live_bytes(database: Path) -> int:
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    pages, free, size = (connection.execute(f"PRAGMA {name}").fetchone()[0] for name in ("page_count", "freelist_count", "page_size"))
    connection.close()
    return (pages - free) * size


def _free_bytes(monkeypatch, free):
    class Usage:
        pass
    Usage.free = free
    monkeypatch.setattr(backup_service.shutil, "disk_usage", lambda _: Usage)
    monkeypatch.setattr(backup_schedule.shutil, "disk_usage", lambda _: Usage)


def test_a_run_refuses_unless_both_copies_fit_and_room_is_left_for_live_writes(database, alerts, monkeypatch):
    estimate = _live_bytes(database)
    required = 2 * estimate + 128 * 1024 * 1024
    directory = database.parent / "backups"
    directory.mkdir()
    old = [_scheduled(directory, NOW - timedelta(days=day)) for day in range(20, 90)]
    # The review's reproduction: more than one copy needs, less than the run really uses.
    _free_bytes(monkeypatch, required - 1)
    with pytest.raises(BackupValidationError):
        create_scheduled_backup(_url(database), now=NOW)
    assert all(path.exists() for path in old), "a refused run must not prune"
    assert not list(directory.glob(f"{PARTIAL_PREFIX}*"))
    assert backup_failure_alerts.backup_failure_reason(alerts[-1])[0] == "disk_space"
    _free_bytes(monkeypatch, required)
    assert create_scheduled_backup(_url(database), now=NOW)["filename"] in _odin_backups(database)


def test_the_restore_test_rechecks_free_space_before_its_copy(database, monkeypatch):
    backup = database.parent / "backup.db"
    backup.write_bytes(database.read_bytes())
    needed = backup.stat().st_size + backup_schedule.LIVE_WRITES_RESERVE_BYTES
    monkeypatch.setattr(backup_schedule.subprocess, "run", lambda *a, **k: pytest.fail("must not start"))
    _free_bytes(monkeypatch, needed - 1)
    with pytest.raises(BackupValidationError, match="free disk space"):
        backup_schedule.rehearse_restore(backup, database)
    assert not list(database.parent.glob(f"{PARTIAL_PREFIX}*"))
    assert backup_failure_alerts.backup_failure_reason(BackupValidationError("Insufficient free disk space for the restore test"))[0] == "disk_space"


def test_a_manual_backup_keeps_its_own_smaller_requirement(database, alerts, monkeypatch):
    """One copy and a smaller margin: it runs when someone asks, and writes no second copy."""
    estimate = _live_bytes(database)
    _free_bytes(monkeypatch, estimate + max(estimate // 4, backup_service.MIN_BACKUP_FREE_BYTES))
    path, _ = create_online_backup(_url(database))
    assert path.name in _odin_backups(database)


# --- Interrupted runs: nothing unverified ever carries the published name.

_KILLED_RUN = """
import os, signal, sys
from datetime import datetime, timezone
from modules.system import backup_failure_alerts, backup_schedule
backup_failure_alerts.publish_backup_failure = lambda error: None
where = sys.argv[2]
die = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
if where == "verify":
    backup_schedule.verify_sqlite_file = die
elif where == "restore-copy":
    original = backup_schedule.shutil.copyfile
    def copy_then_die(source, target):
        original(source, target)
        die()
    backup_schedule.shutil.copyfile = copy_then_die
elif where == "after-receipt":
    original = backup_schedule.os.replace
    def replace(source, target):
        if str(target).endswith(".db"):
            die()
        return original(source, target)
    backup_schedule.os.replace = replace
backup_schedule.create_scheduled_backup("sqlite:///" + sys.argv[1])
"""


@pytest.mark.parametrize("where", ["verify", "restore-copy", "after-receipt"])
def test_a_run_killed_in_any_window_leaves_nothing_listed_and_the_next_run_clears_it(database, alerts, where):
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=f"{root / 'backend'}:{root}", JWT_SECRET_KEY="db-safety-test-only", DATABASE_URL=_url(database))
    killed = subprocess.run([sys.executable, "-c", _KILLED_RUN, str(database), where], cwd=root / "backend", env=env,
                            capture_output=True, text=True, timeout=120)
    assert killed.returncode == -9, killed.stderr[-2000:]
    directory = database.parent / "backups"
    assert _odin_backups(database) == []
    assert list(directory.glob(f"{PARTIAL_PREFIX}*")), "the kill should leave its working files"
    backup_schedule.clear_leftovers(_url(database))
    assert list(directory.iterdir()) == []
    result = create_scheduled_backup(_url(database), now=NOW)
    assert _odin_backups(database) == [result["filename"]]
    assert sorted(path.name for path in directory.iterdir()) == [result["filename"], result["filename"] + RECEIPT_SUFFIX]


def test_the_published_name_only_appears_once_everything_has_passed(database, alerts, monkeypatch):
    seen = []
    original = backup_schedule._write_receipt
    def rehearse(backup, live):
        seen.append(("rehearse", backup.name, _odin_backups(database)))
    def receipt(final, *args):
        seen.append(("receipt", final.name, _odin_backups(database)))
        original(final, *args)
    monkeypatch.setattr(backup_schedule, "rehearse_restore", rehearse)
    monkeypatch.setattr(backup_schedule, "_write_receipt", receipt)
    result = create_scheduled_backup(_url(database), now=NOW)
    assert seen[0][0] == "rehearse" and seen[0][1].startswith(PARTIAL_PREFIX) and seen[0][2] == []
    assert seen[1][0] == "receipt" and seen[1][1] == result["filename"] and seen[1][2] == []


def test_a_rename_that_cannot_be_made_durable_counts_as_failed(database, alerts, monkeypatch):
    """Otherwise a crash could lose it after ODIN had counted it. The old copies stay."""
    directory = database.parent / "backups"
    directory.mkdir()
    old = [_scheduled(directory, NOW - timedelta(days=day)) for day in range(20, 90)]
    original = backup_schedule._fsync_directory
    def fail_after_publish(path):
        if any(item.name.startswith("odin_backup_") and item.suffix == ".db" and item not in old for item in path.iterdir()):
            raise OSError(5, "synthetic")
        return original(path)
    monkeypatch.setattr(backup_schedule, "_fsync_directory", fail_after_publish)
    before = _odin_backups(database)
    with pytest.raises(OSError):
        create_scheduled_backup(_url(database), now=NOW)
    assert _odin_backups(database) == before and all(path.exists() for path in old)
    assert sum(path.name.endswith(RECEIPT_SUFFIX) for path in directory.iterdir()) == len(old)
    assert alerts


def test_reading_the_list_never_removes_a_receipt(tmp_path):
    """The off-server export reads this without the restore lock, while a run may be publishing."""
    directory = tmp_path / "backups"
    directory.mkdir()
    backup = _scheduled(directory, NOW)
    receipt = backup.with_name(backup.name + RECEIPT_SUFFIX)
    backup.unlink()
    assert backup_schedule._scheduled_backups(directory) == []
    assert receipt.exists()


# --- The restore test keeps its evidence and can't hang the guardian.

def test_a_hung_restore_test_times_out_and_cleans_up(database, monkeypatch):
    monkeypatch.setattr(backup_schedule, "RESTORE_TEST_SECONDS", 0.01)
    copy = database.parent / "copy.db"
    copy.write_bytes(database.read_bytes())
    with pytest.raises(BackupValidationError, match="timed out"):
        backup_schedule.rehearse_restore(copy, database)
    assert not list(database.parent.glob(f"{PARTIAL_PREFIX}*"))


def test_a_refused_restore_test_keeps_startups_reason_in_the_log(database, caplog):
    bad = database.parent / "bad.db"
    bad.write_bytes(database.read_bytes())
    overwrite_rightmost_leaf(bad, "audit_logs", "printers")
    with caplog.at_level("ERROR", logger="odin.backup_schedule"), pytest.raises(BackupValidationError, match="startup refused") as caught:
        backup_schedule.rehearse_restore(bad, database)
    assert "failed its integrity check" in caplog.text
    # The alert text is chosen from the exception, so startup's words can't reach it or misclassify it.
    assert "integrity" not in str(caught.value) and backup_failure_alerts.backup_failure_reason(caught.value)[0] == "restore_test"


# --- A restore upload must not freeze the API while a backup holds the lock.

def test_staging_a_restore_runs_off_the_event_loop(tmp_path, monkeypatch):
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
    result = asyncio.run(routes_backup.restore_backup(file=Upload(), current_user={"id": 1}, db=None))
    assert result["status"] == "staged" and seen == ["in a worker thread"]


def test_staging_a_postgresql_restore_runs_off_the_event_loop(tmp_path, monkeypatch):
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
    result = asyncio.run(routes_backup.restore_backup(file=Upload(), current_user={"id": 7}, db=None))
    assert result["status"] == "staged"
    assert seen == ["in a worker thread", ("maintenance-url", 7, "app-file", "maintenance-file")]


# --- Backup alerts take the phone path, as the integrity alert does.

@pytest.fixture
def alert_engine(tmp_path):
    from core.schema import bootstrap_database
    engine = create_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    bootstrap_database(engine)
    with engine.begin() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        connection.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES"
                                "(1,'admin','x','admin',1,NULL),(2,'muted','x','admin',1,NULL),(3,'viewer','x','viewer',1,NULL)"))
    yield engine, sessionmaker(bind=engine)
    engine.dispose()


def _alert_rows(engine):
    with engine.connect() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        return connection.execute(text("SELECT user_id, title, message FROM alerts ORDER BY user_id")).all()


@pytest.mark.parametrize("error,start", [
    (BackupValidationError("Insufficient free disk space for a verified backup"), "Backup refused"),
    (backup_service.BackupMissedError("missed"), "Backup missed"),
    (backup_service.BackupMissedError("offsite_missed"), "Backup missed"),
], ids=["refused", "missed", "offsite-missed"])
def test_backup_alerts_reach_the_phone_path_after_the_row_is_saved(alert_engine, monkeypatch, error, start):
    from modules.notifications import alert_outbox
    engine, factory = alert_engine
    delivered = []
    def mobile(db, plan):
        delivered.append((plan.title, plan.message, str(plan.severity.value), sorted(plan.in_app_users), len(_alert_rows(engine))))
    monkeypatch.setattr(alert_outbox, "deliver_mobile", mobile)
    backup_failure_alerts.publish_backup_failure(error, session_factory=factory, now=NOW)
    assert len(delivered) == 1
    title, message, severity, users, rows_when_delivered = delivered[0]
    assert title == "Database backup failed" and message.startswith(start) and severity == "critical"
    assert users == [1, 2] and rows_when_delivered == 2, "both rows saved before anything is sent"
    # The daily limit covers the phone as well as the inbox.
    backup_failure_alerts.publish_backup_failure(error, session_factory=factory, now=NOW + timedelta(hours=23))
    assert len(delivered) == 1 and len(_alert_rows(engine)) == 2
    backup_failure_alerts.publish_backup_failure(error, session_factory=factory, now=NOW + timedelta(days=1, seconds=1))
    assert len(delivered) == 2 and len(_alert_rows(engine)) == 4


def test_an_admin_already_told_today_is_not_told_again_when_another_is(alert_engine, monkeypatch):
    from modules.notifications import alert_outbox
    engine, factory = alert_engine
    delivered = []
    monkeypatch.setattr(alert_outbox, "deliver_mobile", lambda db, plan: delivered.append(sorted(plan.in_app_users)))
    error = BackupValidationError("Insufficient free disk space")
    backup_failure_alerts.publish_backup_failure(error, session_factory=factory, now=NOW)
    with engine.begin() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        connection.execute(text("DELETE FROM alerts WHERE user_id=2"))
    backup_failure_alerts.publish_backup_failure(error, session_factory=factory, now=NOW + timedelta(hours=1))
    assert delivered == [[1, 2], [2]]
    assert [row[0] for row in _alert_rows(engine)] == [1, 2]


def test_a_phone_that_cannot_be_reached_leaves_the_inbox_row_saved(alert_engine, monkeypatch):
    from modules.notifications import alert_dispatcher
    engine, factory = alert_engine
    def unreachable(db, plan):
        raise OSError("push service unreachable")
    monkeypatch.setattr(alert_dispatcher, "deliver_staged_alert", unreachable)
    with pytest.raises(OSError):
        backup_failure_alerts.publish_backup_failure(BackupValidationError("Insufficient free disk space"), session_factory=factory, now=NOW)
    assert [row[0] for row in _alert_rows(engine)] == [1, 2]


def test_a_muted_preference_still_gets_the_backup_alerts_inbox_row(alert_engine, monkeypatch):
    from modules.notifications import alert_dispatcher
    engine, factory = alert_engine
    monkeypatch.setattr(alert_dispatcher, "deliver_staged_alert", lambda db, plan: None)
    with factory() as db:
        alert_dispatcher.seed_alert_preferences(db, 2)
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        db.execute(text("UPDATE alert_preferences SET in_app=0 WHERE user_id=2 AND lower(alert_type)='maintenance_overdue'"))
        db.commit()
    backup_failure_alerts.publish_backup_failure(BackupValidationError("Insufficient free disk space"), session_factory=factory, now=NOW)
    assert [row[0] for row in _alert_rows(engine)] == [1, 2]
