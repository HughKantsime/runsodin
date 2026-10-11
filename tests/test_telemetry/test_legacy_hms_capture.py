"""Actual history persistence; notification/job effects are tested separately."""
import sqlite3
import pytest
from tests.test_telemetry.test_legacy_monitor_capture import capture_store, rows

ERROR = [{'attr':0x05000200,'code':0x0003000A}]

@pytest.fixture
def hms_capture(capture_store, monkeypatch):
    from modules.notifications import event_dispatcher
    monkeypatch.setattr(event_dispatcher,'process_hms_errors',lambda *args,**kwargs:None)
    return capture_store


def test_hms_history_translates_and_deduplicates_unchanged_errors(hms_capture):
    path, clock, send = hms_capture
    send(hms=ERROR)
    first = rows(path,'hms_error_history')
    assert len(first)==1
    assert first[0][1:6] == (1,'05000200_0003000A','AMS Hub: cutter failure.','warning','bambu_hms')
    clock[0]=1010
    send(hms=ERROR)
    assert rows(path,'hms_error_history')==first


def test_hms_history_records_same_error_after_explicit_clear(hms_capture):
    path, clock, send = hms_capture
    send(hms=ERROR)
    clock[0]=1010
    send(hms=[])
    clock[0]=1020
    send(hms=ERROR)
    assert len(rows(path,'hms_error_history'))==2


def test_hms_history_failed_write_retries_unchanged_error(hms_capture):
    path, clock, send = hms_capture
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER reject_history BEFORE INSERT ON hms_error_history BEGIN SELECT RAISE(ABORT,'synthetic history failure'); END")
    send(hms=ERROR)
    assert rows(path,'hms_error_history')==[]
    with sqlite3.connect(path) as db:
        db.execute('DROP TRIGGER reject_history')
    clock[0]=1010
    send(hms=ERROR)
    assert len(rows(path,'hms_error_history'))==1


def test_hms_clear_inside_heartbeat_throttle_still_allows_reappearance(hms_capture):
    path, clock, send = hms_capture
    send(hms=ERROR)
    clock[0]=1001
    send(hms=[])
    clock[0]=1010
    send(hms=ERROR)
    assert len(rows(path,'hms_error_history'))==2


@pytest.mark.parametrize('missing', [True,False])
def test_missing_or_null_hms_does_not_invent_clear(hms_capture, missing):
    path, clock, send = hms_capture
    send(hms=ERROR)
    clock[0]=1001
    if missing:
        send()
    else:
        send(hms=None)
    clock[0]=1010
    send(hms=ERROR)
    assert len(rows(path,'hms_error_history'))==1


def test_second_history_insert_failure_rolls_back_batch_and_retries(hms_capture):
    path, clock, send = hms_capture
    batch = ERROR + [{'attr':0x0C000100,'code':0x00010004}]
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER reject_second_history BEFORE INSERT ON hms_error_history WHEN NEW.code='0C000100_00010004' BEGIN SELECT RAISE(ABORT,'synthetic second error failure'); END")
    send(hms=batch)
    assert rows(path,'hms_error_history')==[]
    with sqlite3.connect(path) as db:
        db.execute('DROP TRIGGER reject_second_history')
    clock[0]=1010
    send(hms=batch)
    assert [row[2] for row in rows(path,'hms_error_history')] == ['05000200_0003000A','0C000100_00010004']


def test_history_retention_failure_preserves_old_row_and_retries(hms_capture):
    path, clock, send = hms_capture
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO hms_error_history(printer_id,code,occurred_at) VALUES(1,'synthetic-old',datetime('now','-91 days'))")
        db.execute("CREATE TRIGGER reject_history_cleanup BEFORE DELETE ON hms_error_history BEGIN SELECT RAISE(ABORT,'synthetic history cleanup failure'); END")
    original=rows(path,'hms_error_history')
    send(hms=ERROR)
    assert rows(path,'hms_error_history')==original
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT gcode_state FROM printers WHERE id=1').fetchone()[0]=='IDLE'
        db.execute('DROP TRIGGER reject_history_cleanup')
    clock[0]=1010
    send(hms=ERROR)
    assert [row[2] for row in rows(path,'hms_error_history')]==['05000200_0003000A']


def test_history_cleanup_preserves_eighty_nine_days_removes_ninety_one(hms_capture):
    path, clock, send = hms_capture
    with sqlite3.connect(path) as db:
        for age in (89,91):
            db.execute("INSERT INTO hms_error_history(printer_id,code,occurred_at) VALUES(1,?,datetime('now',?))", (f'synthetic-{age}',f'-{age} days'))
    send(hms=ERROR)
    assert [row[2] for row in rows(path,'hms_error_history')]==['synthetic-89','05000200_0003000A']
