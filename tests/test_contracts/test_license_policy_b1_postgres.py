"""B1 real PostgreSQL CAS, vacancy and recovery transaction parity."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_controlled_postgres import native_pg


def test_postgres_selection_concurrency_vacancy_and_handoff(native_pg, tier_license, monkeypatch):
    from core.schema import bootstrap_database
    from core.auth import hash_password
    from core import license_lock as policy
    bootstrap_database(native_pg)
    with native_pg.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(1,'one',:h,'admin',TRUE),(2,'two',:h,'admin',TRUE)"), {'h': hash_password('SyntheticPass123!')})
        c.execute(text("INSERT INTO printers(id,name,is_active) VALUES(1,'one',TRUE),(2,'two',TRUE)"))
    tier_license('enterprise', max_users=1, max_printers=1)
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    with Session(native_pg) as db:
        initial = policy.reconcile(db); db.commit()
    def save(number):
        with Session(native_pg) as db:
            try:
                p = policy.replace_selection(db, users=[number], printers=[number], recovery_user_id=number,
                                             expected_revision=initial['revision'], actor=1)
                db.commit(); return 200, p
            except policy.LicenseLockError as exc:
                db.rollback(); return exc.status_code, exc.detail
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(save, [1, 2]))
    assert sorted(r[0] for r in result) == [200, 409]
    winner = next(p for code, p in result if code == 200)
    with Session(native_pg) as db:
        db.execute(text('UPDATE printers SET is_active=FALSE WHERE id=:id'), {'id': winner['printers'][0]}); db.commit()
        p = policy.reconcile(db); db.commit()
        assert p['printers'] != winner['printers']
        assert p['membership_generation'] > winner['membership_generation']
        db.execute(text("UPDATE users SET password_hash='' WHERE id=:id"), {'id': 3 - winner['users'][0]}); db.commit()
        db.execute(text('DELETE FROM users WHERE id=:id'), {'id': winner['users'][0]})
        with pytest.raises(policy.LicenseLockError): db.commit()
        db.rollback()


def test_postgres_print_admission_linearizes_with_selection(native_pg, tier_license, monkeypatch):
    """B2a: a change holding the policy row lock is ordered before admission."""
    import threading
    import time
    from core.schema import bootstrap_database
    from core.auth import hash_password
    from core import license_lock as policy
    bootstrap_database(native_pg)
    with native_pg.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(1,'one',:h,'admin',TRUE)"),
                  {'h': hash_password('SyntheticPass123!')})
        c.execute(text("INSERT INTO printers(id,name,is_active) VALUES(1,'one',TRUE),(2,'two',TRUE)"))
        for printer, token in ((1, 'p' * 32), (2, 'q' * 32)):
            c.execute(text("INSERT INTO jobs(id,item_name,status,priority,hold,quantity,created_at,updated_at) "
                           "VALUES(:p,'part','pending',3,FALSE,1,now(),now())"), {'p': printer})
            c.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,"
                           "payload_sha256,token,state) VALUES(:p,:p,:k,:k,:k,:t,'adapter_entered')"),
                      {'p': printer, 't': token, 'k': '0' * 64})
    tier_license('enterprise', max_users=1, max_printers=1)
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    policy._cache.clear()
    with Session(native_pg) as db:
        initial = policy.reconcile(db); db.commit()
    assert initial['printers'] == [1]
    # Admission first: recorded, and kept when the printer is then locked.
    assert policy.admit_start(native_pg, printer_id=1, job_id=1, claim_token='p' * 32) is None
    with Session(native_pg) as db:
        assert set(policy._read(db, policy.ADMITTED_KEY)) == {policy._attempt_key('p' * 32)}
    # Selection change in flight: admission waits on the row lock, then refuses.
    changer = Session(native_pg)
    current = policy.selection_status(changer); changer.commit()
    policy.replace_selection(changer, printers=[2], users=[1], expected_revision=current['revision'], actor=1)
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault(
        'r', policy.admit_start(native_pg, printer_id=1, job_id=1, claim_token='p' * 32)))
    worker.start()
    time.sleep(0.5)
    assert worker.is_alive()
    changer.commit(); changer.close()
    worker.join(10)
    assert result['r'] is not None and result['r'].detail['code'] == 'license_printer_locked'
    assert policy.admit_start(native_pg, printer_id=2, job_id=2, claim_token='q' * 32) is None


def test_postgres_exempt_attempt_capture_and_retire(native_pg, tier_license, monkeypatch):
    """B2b-1: a fresh running print on a newly locked printer is captured, then retired."""
    from datetime import datetime, timedelta, timezone
    from core.schema import bootstrap_database
    from core.auth import hash_password
    from core.economic_source_creation import _create_attempt
    from core import license_lock as policy
    bootstrap_database(native_pg)
    now = datetime.now(timezone.utc)
    with native_pg.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(1,'one',:h,'admin',TRUE)"),
                  {'h': hash_password('SyntheticPass123!')})
        c.execute(text("INSERT INTO printers(id,name,is_active,gcode_state,last_seen) "
                       "VALUES(1,'one',TRUE,'RUNNING',:t),(2,'two',TRUE,'IDLE',:t)"), {'t': now})
    with Session(native_pg) as db:
        db.execute(text('SELECT 1'))
        _create_attempt(db, {'printer_id': 1, 'status': 'running', 'filename': 'part.3mf',
                             'started_at': now - timedelta(minutes=10), 'observed_received_at': now})
        db.commit()
    tier_license('enterprise', max_users=1, max_printers=1)
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    policy._cache.clear()
    with Session(native_pg) as db:
        initial = policy.reconcile(db); db.commit()
    assert initial['printers'] == [1]
    with Session(native_pg) as db:
        p = policy.replace_selection(db, printers=[2], users=[1], expected_revision=initial['revision'], actor=1)
        db.commit()
    assert p['exempt_attempts']['1']['kind'] == 'observed'
    with Session(native_pg) as db:
        snap = policy._snapshot(p)
        assert policy.attempt_allowed(db, snap, 1, 'V') and not policy.printer_allowed(snap, 1, 'N')
        db.execute(text("UPDATE print_jobs SET status='completed', ended_at=now() WHERE printer_id=1"))
        db.commit()
        assert policy.reconcile(db)['exempt_attempts']['1']['kind'] == 'completion'
        db.commit()
