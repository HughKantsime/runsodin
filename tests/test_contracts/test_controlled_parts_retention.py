"""Controlled retention uses real sealed retirement and filesystem receipts."""
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_timelapse_lifetime_membership import prepared_source, encoder, frame_writer, review_db, owned_runtime
from core.controlled_retention import expired, RetentionPrincipal
from modules.archives.controlled_retention import purge_due


def mark(engine, tid):
    with engine.begin() as c:
        c.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')"))
        c.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at,purge_previewed_at) VALUES(444,'CUI',1,'Secure',1,'fixture',:time,:time)"), {'time': (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()})
        c.execute(text('UPDATE timelapses SET completed_at=:time WHERE id=:id'),
                  {'time': datetime.now(timezone.utc) - timedelta(days=2), 'id': tid})


def test_purge_sealed_media_and_audit(prepared_source):
    engine, root, tid, _, nonce = prepared_source
    mark(engine, tid)
    assert purge_due(engine, root) == 1
    with Session(engine) as db:
        assert not db.execute(text('SELECT 1 FROM timelapses WHERE id=:id'), {'id': tid}).first()
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='controlled_part.timelapse_purged'")).scalar() == 1
    assert purge_due(engine, root) == 0


def test_no_early_purge_or_unmarked_changes(prepared_source):
    engine, root, tid, _, nonce = prepared_source
    assert purge_due(engine, root) == 0
    mark(engine, tid)
    with Session(engine) as db:
        db.execute(text('UPDATE controlled_jobs SET marked_at=:time WHERE job_id=444'), {'time': datetime.now(timezone.utc).isoformat()})
        db.commit()
        assert not expired(db, tid)
    assert purge_due(engine, root) == 0
    with pytest.raises(ValueError):
        RetentionPrincipal(tid, nonce, proof=object())


def test_mark_time_delays_old_media_and_requires_preview(prepared_source):
    engine, root, tid, _, nonce = prepared_source
    mark(engine, tid)
    with engine.begin() as c:
        c.execute(text('UPDATE controlled_jobs SET marked_at=:now,purge_previewed_at=NULL'), {'now': datetime.now(timezone.utc).isoformat()})
    with Session(engine) as db:
        assert not expired(db, tid)
    with engine.begin() as c:
        c.execute(text('UPDATE controlled_jobs SET marked_at=:old'), {'old': (datetime.now(timezone.utc)-timedelta(days=3)).isoformat()})
    assert purge_due(engine, root) == 0
    with engine.begin() as c:
        c.execute(text('UPDATE controlled_jobs SET purge_previewed_at=:now'), {'now': datetime.now(timezone.utc).isoformat()})
    # Media content (not just the row) must disappear from disk.
    before = [p for p in root.rglob('*') if p.is_file() and p.suffix in ('.jpg', '.mp4')]
    assert before
    assert purge_due(engine, root) == 1
    assert all(not p.exists() for p in before)


def test_failed_retention_pass_advances_before_retry(monkeypatch):
    import modules.archives.controlled_retention as worker
    waits = []
    class Stop:
        def is_set(self): return len(waits) >= 2
        def wait(self, delay): waits.append(delay)
    def fail(*args, **kwargs): raise RuntimeError('owned failure')
    monkeypatch.setattr(worker, 'purge_due', fail)
    worker.retention_loop(None, None, Stop())
    assert waits == [300, 300]


def test_retention_uses_minimum_across_policies(prepared_source, monkeypatch):
    # Pin the evaluation clock, far from either deadline. A slow suite cannot
    # move the long policy across expiry while this test is retiring real bytes.
    now = datetime.now(timezone.utc)
    class Clock:
        @staticmethod
        def now(tz=None): return now
    monkeypatch.setattr('core.controlled_retention.datetime', Clock)
    engine, root, tid, _, nonce = prepared_source
    mark(engine, tid)
    with engine.begin() as c:
        file_id=c.execute(text('SELECT print_file_id FROM timelapse_ownership_anchors WHERE operation_id=:nonce'),{'nonce':nonce}).scalar_one()
        c.execute(text("INSERT INTO controlled_files(file_id,label,audience_id,printer_tag,retention_days,reason,marked_at,purge_previewed_at) VALUES(:file,'CUI',1,'Secure',30,'fixture',:old,:old)"),{'file':file_id,'old':(datetime.now(timezone.utc)-timedelta(days=3)).isoformat()})
    with Session(engine) as db:
        assert not expired(db,tid,now=now-timedelta(days=2))
        assert expired(db,tid,now=now)
    before = [p for p in root.rglob('*') if p.is_file() and p.suffix in ('.jpg', '.mp4')]
    assert before
    assert purge_due(engine,root)==1
    assert all(not p.exists() for p in before)


def test_due_unacknowledged_notice(prepared_source):
    from core.controlled_retention import pending_notice
    engine, root, tid, _, nonce=prepared_source
    mark(engine,tid)
    with engine.begin() as c:c.execute(text('UPDATE controlled_jobs SET purge_previewed_at=NULL'))
    with Session(engine) as db:
        notices=pending_notice(db)
        assert len(notices)==1 and notices[0]['kind']=='controlled_retention_pending'
    assert purge_due(engine,root)==0


def test_controlled_legacy_timelapse_bytes_purged(review_db,tmp_path):
    from core.schema import bootstrap_database
    from sqlalchemy import create_engine
    engine=create_engine('sqlite:///'+str(tmp_path/'legacy.db'));bootstrap_database(engine)
    root=tmp_path/'media';root.mkdir(mode=0o700)
    (root/'legacy.mp4').write_bytes(b'legacy');(root/'5').mkdir();(root/'5'/'frame.jpg').write_bytes(b'frame')
    with engine.begin() as c:
        c.execute(text("INSERT INTO printers(id,name) VALUES(1,'legacy')"))
        c.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(444,'legacy','completed')"))
        c.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status) VALUES(5,1,444,'legacy.mp4','ready')"))
    mark(engine,5)
    assert purge_due(engine,root)==1
    assert not (root/'legacy.mp4').exists() and not (root/'5').exists()
    engine.dispose()


def test_deleted_job_acknowledged_tombstone_retires_real_media(prepared_source):
    from core.controlled_deletion import redact_deleted_job_history
    from core.controlled_retention import pending_notice
    engine, root, tid, _, nonce = prepared_source
    mark(engine, tid)
    before = [p for p in root.rglob('*') if p.is_file() and p.suffix in ('.jpg', '.mp4')]
    assert before
    with Session(engine) as db:
        saved = db.execute(text('SELECT purge_previewed_at FROM controlled_jobs WHERE job_id=444')).scalar_one()
        redact_deleted_job_history(db, 444)
        db.execute(text('DELETE FROM controlled_jobs WHERE job_id=444'))
        db.execute(text('DELETE FROM jobs WHERE id=444')); db.commit()
        assert db.execute(text('SELECT purge_previewed_at FROM controlled_job_tombstones WHERE job_id=444')).scalar_one() == saved
        assert pending_notice(db) == []
    assert purge_due(engine, root) == 1
    assert all(not p.exists() for p in before)
    with Session(engine) as db:
        assert not db.execute(text('SELECT id FROM timelapses WHERE id=:id'), {'id': tid}).first()
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='controlled_part.timelapse_purged'")).scalar_one() == 1
