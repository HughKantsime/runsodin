"""Physical print identity, terminal truth, and closed-unknown replay contracts."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from contextlib import contextmanager
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db


def observe(db, name, elapsed):
    from core.attempt_start_observation import observe_start
    return observe_start(db.connection(), printer_id=9, protocol='moonraker', filename=name,
        job_name=name, physical_values={'started_at':datetime.now(timezone.utc).isoformat()},
        source='contract-start', allow_physical_resume=True, observed_elapsed=elapsed)


@pytest.mark.parametrize('same_file', [False, True])
def test_old_running_rows_are_closed_unknown_and_never_archived(review_db, monkeypatch, same_file):
    from core.physical_print import CLOSED_UNKNOWN
    from modules.archives import archive
    engine = review_db.get_bind()
    @contextmanager
    def owned_db():
        conn=engine.raw_connection()
        try:yield conn
        except BaseException:conn.rollback();raise
        finally:conn.close()
    monkeypatch.setattr(archive, 'get_db', owned_db)
    for n in range(3):
        review_db.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,status,started_at) VALUES(9,:name,:name,'running','2026-03-14T12:00:00')"), {'name':'new.gcode' if same_file else 'old.gcode'})
    result=observe(review_db,'new.gcode',60)
    assert result.fresh
    review_db.commit()
    old=review_db.execute(text('SELECT id,status,ended_at,error_code FROM print_jobs WHERE id<>:id'),{'id':result.id}).all()
    assert len(old)==3
    for row in old:
        assert tuple(row[1:])==('cancelled',None,CLOSED_UNKNOWN)
        assert archive.create_print_archive(row.id,9,False) is None
    assert archive.recover_missing_archives()['attempted']==0
    assert review_db.execute(text('SELECT COUNT(*) FROM print_archives')).scalar_one()==0
    assert review_db.execute(text('SELECT COUNT(*) FROM spool_usage')).scalar_one()==0


def test_same_file_recent_physical_resume_adopts(review_db):
    started=(datetime.now(timezone.utc)-timedelta(seconds=65)).isoformat()
    identifier=review_db.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,status,started_at) VALUES(9,'part.gcode','part.gcode','running',:start) RETURNING id"),{'start':started}).scalar_one()
    result=observe(review_db,'part.gcode',60)
    assert not result.fresh and result.id==identifier
    assert review_db.execute(text('SELECT COUNT(*) FROM print_jobs')).scalar_one()==1


def test_reconnect_does_not_adopt_database_rows(review_db, monkeypatch):
    from modules.printers.monitors import moonraker_monitor as moon
    monkeypatch.setattr(moon,'engine',review_db.get_bind())
    review_db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at) VALUES(9,'old.gcode','running','2026-03-14')"))
    review_db.commit()
    monitor=moon.MoonrakerMonitor(9,'Owned','127.0.0.1')
    monitor._recover_after_reconnect()
    assert monitor._current_job_db_id is None and monitor._prev_state is None


def test_history_cancelled_overrides_standby_completion(review_db,monkeypatch):
    from modules.printers.monitors import moonraker_monitor as moon
    from modules.printers.monitors.moonraker_history import terminal_history
    from modules.archives import archive
    from core.registry import registry
    monkeypatch.setattr(moon,'engine',review_db.get_bind())
    monkeypatch.setattr(registry,'get_provider',lambda name: None)
    monkeypatch.setattr(archive,'create_print_archive',lambda **kwargs: None)
    import modules.notifications.event_dispatcher as events
    monkeypatch.setattr(events,'dispatch_alert',lambda **kwargs: None)
    monkeypatch.setattr(events,'record_error',lambda **kwargs: None)
    start=datetime.now(timezone.utc)-timedelta(hours=1)
    end=datetime.now(timezone.utc)
    identifier=review_db.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,status,started_at) VALUES(9,'part.gcode','part.gcode','running','2026-03-14') RETURNING id")).scalar_one()
    review_db.commit()
    printer=SimpleNamespace(get_job_history=lambda limit:[dict(filename='part.gcode',status='cancelled',start_time=start.timestamp(),end_time=end.timestamp(),print_duration=3500)])
    history=terminal_history(printer,'part.gcode',end.timestamp())
    monitor=moon.MoonrakerMonitor(9,'Owned','127.0.0.1')
    monitor._current_job_db_id=identifier;monitor._last_filename='part.gcode'
    monitor._job_ended('completed',SimpleNamespace(filename='part.gcode',raw_print_state='standby',error_message='',filament_used_mm=0),history=history)
    row=review_db.execute(text('SELECT status,started_at,ended_at,print_duration_seconds FROM print_jobs WHERE id=:id'),{'id':identifier}).one()
    assert tuple(row)==('cancelled',history['started_at'],history['ended_at'],3500)


@pytest.mark.parametrize('mode',['ambiguous','wrong_name','old','unavailable','malformed'])
def test_history_refuses_ambiguous_or_unavailable(mode):
    from modules.printers.monitors.moonraker_history import terminal_history
    job=dict(filename='part.gcode',status='cancelled',start_time=1000,end_time=2000,print_duration=900)
    jobs=[job]
    if mode=='ambiguous':jobs=[job,dict(job)]
    if mode=='wrong_name':job['filename']='another.gcode'
    if mode=='old':job['end_time']=1500
    if mode=='malformed':job['end_time']='invalid'
    def history(limit):
        if mode=='unavailable':raise RuntimeError('owned offline')
        return jobs
    assert terminal_history(SimpleNamespace(get_job_history=history),'part.gcode',2000) is None


def test_terminal_history_fetch_does_not_hold_status_lock(monkeypatch):
    from modules.printers.monitors import moonraker_monitor as moon
    monitor=moon.MoonrakerMonitor(9,'Owned','127.0.0.1')
    monitor._current_job_db_id=1;monitor._last_filename='part.gcode';monitor._prev_state='RUNNING'
    now=datetime.now(timezone.utc).timestamp()
    def fetch(limit):
        assert not monitor._status_lock._is_owned()
        return [dict(filename='part.gcode',status='cancelled',start_time=now-600,end_time=now,print_duration=590)]
    monitor.printer=SimpleNamespace(get_job_history=fetch)
    received=[]
    monkeypatch.setattr(monitor,'_process_status_locked',lambda status,history=None:received.append(history))
    monitor._process_status(SimpleNamespace(internal_state='IDLE',raw_print_state='standby'))
    assert received[0]['status']=='cancelled'


from tests.test_contracts.test_archive_persistence_accounting_behavior import archive_case


def test_archive_replay_returns_original_archive_without_a_duplicate(archive_case):
    archive,engine=archive_case
    first=archive.create_print_archive(7099,9,True)
    assert first is not None
    assert archive.create_print_archive(7099,9,True)==first
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM print_archives WHERE print_job_id=7099')).scalar_one()==1


def test_education_running_row_is_never_closed_by_the_fallback(review_db):
    from core.physical_print import CLOSED_UNKNOWN
    review_db.commit()
    review_db.execute(text('PRAGMA foreign_keys=OFF'))  # setup only: an Education-owned running row
    review_db.execute(text('UPDATE education_submissions SET job_id=813 WHERE id=(SELECT MIN(id) FROM education_submissions)'))
    owned=review_db.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,status,started_at,scheduled_job_id) VALUES(9,'old.gcode','old.gcode','running','2026-03-14T12:00:00',813) RETURNING id")).scalar_one()
    stale=review_db.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,status,started_at) VALUES(9,'old.gcode','old.gcode','running','2026-03-14T12:00:00') RETURNING id")).scalar_one()
    review_db.commit()
    review_db.execute(text('PRAGMA foreign_keys=ON'))
    result=observe(review_db,'new.gcode',60)
    assert result.fresh
    rows=dict(review_db.execute(text('SELECT id,status FROM print_jobs WHERE id IN (:a,:b)'),{'a':owned,'b':stale}).all())
    assert rows=={owned:'running',stale:'cancelled'}
    assert review_db.execute(text('SELECT error_code FROM print_jobs WHERE id=:id'),{'id':stale}).scalar_one()==CLOSED_UNKNOWN


def test_closed_unknown_rows_have_no_open_end_in_overlap_queries():
    import inspect
    from core import job_timeline
    from modules.jobs import scheduler_routes
    # Only a running row may stand for "still printing"; a closed-unknown row (ended_at NULL) must not match.
    assert "(ended_at IS NULL AND status='running')" in inspect.getsource(job_timeline)
    assert "(pj.ended_at IS NULL AND pj.status = 'running')" in inspect.getsource(scheduler_routes)
