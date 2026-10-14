"""Owned durable first-observation authority, refusal and contention evidence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
import secrets
import pytest
from sqlalchemy import text
from core.dispatch_observations import bind_generic_observation
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch

@pytest.fixture
def observed(review_db,tmp_path,monkeypatch):
    from core import auth
    from modules.printers import dispatch
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch)
    names=[]
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda jid,path,name,creds:(names.append(name) or True,'Synthetic accepted'))
    assert dispatch.dispatch_job(59,53)[0]
    with engine.begin() as conn:
        for identifier in (70,71):
            conn.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,scheduled_job_id,status,started_at,observed_received_at) VALUES(:id,59,:name,:name,53,'running',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),{'id':identifier,'name':names[0]})
    return engine,auth.SECRET_KEY.encode()


def binding(engine):
    with engine.connect() as conn:return conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one()


def test_first_observation_same_id_idempotence_and_reopen(observed):
    engine,key=observed
    for _ in range(2):assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
    engine.dispose()
    assert binding(engine)==70
    assert bind_generic_observation(engine,printer_id=59,print_job_id=71,key=key)=='refused'
    assert binding(engine)==70

@pytest.mark.parametrize('statement',[
    "UPDATE print_jobs SET printer_id=9 WHERE id=70",
    "UPDATE print_jobs SET scheduled_job_id=54 WHERE id=70",
    "UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=70",
    "UPDATE print_jobs SET status='completed' WHERE id=70",
    "UPDATE print_jobs SET filename='reused.gcode' WHERE id=70",
    "UPDATE print_jobs SET filename='odin-dispatch-ffffffffffffffffffffffffffffffff.gcode' WHERE id=70",
    "UPDATE printers SET plug_auth_token='synthetic replacement' WHERE id=59",
    "UPDATE printers SET plug_auto_on=TRUE WHERE id=59",
    "UPDATE printers SET api_host='changed.example.test' WHERE id=59",
    "UPDATE printers SET is_active=FALSE WHERE id=59",
    "UPDATE printer_dispatch_claims SET state='preparing' WHERE printer_id=59",
    "DELETE FROM dispatch_attempt_receipts",
])
def test_mismatched_or_missing_authority_refuses_without_retiring_claim(observed,statement):
    engine,key=observed
    with engine.begin() as conn:conn.execute(text(statement))
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='refused'
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims WHERE printer_id=59')).scalar_one()==1
        assert conn.execute(text('SELECT COUNT(*) FROM dispatch_attempt_receipts WHERE observed_print_job_id IS NOT NULL')).scalar_one()==0

@pytest.mark.parametrize('age',[120,-120])
def test_stale_or_future_start_refuses_without_expiring_claim(observed,age):
    engine,key=observed
    with engine.begin() as conn:conn.execute(text('UPDATE print_jobs SET observed_received_at=:stamp WHERE id=70'),{'stamp':datetime.now(timezone.utc)-timedelta(seconds=age)})
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='refused'
    assert binding(engine) is None

@pytest.mark.parametrize('key',[None,b'short',False])
def test_missing_or_invalid_key_refuses(observed,key):
    engine,_=observed
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='refused'
    assert binding(engine) is None


def test_changed_valid_key_refuses(observed):
    engine,_=observed
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=secrets.token_bytes(32))=='refused'
    assert binding(engine) is None


def test_orphan_receipt_does_not_supply_claim_authority(observed):
    engine,key=observed
    with engine.begin() as conn:conn.execute(text('DELETE FROM printer_dispatch_claims'))
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='no_claim'
    assert binding(engine) is None


def test_simultaneous_first_observations_bind_only_one_id(observed):
    engine,key=observed
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(bind_generic_observation,engine,printer_id=59,print_job_id=identifier,key=key) for identifier in (70,71)]
        results=[future.result(timeout=5) for future in futures]
    assert sorted(results)==['matched','refused']
    assert binding(engine) in (70,71)


def test_database_created_observation_does_not_depend_on_application_clock(observed,monkeypatch):
    from core import dispatch_observations
    engine,key=observed
    with engine.connect() as conn:
        started=dispatch_observations._utc(conn.execute(text('SELECT started_at FROM print_jobs WHERE id=70')).scalar_one())
    class BehindDatabaseClock(datetime):
        @classmethod
        def now(cls,tz=None):return started-timedelta(milliseconds=200)
    monkeypatch.setattr(dispatch_observations,'datetime',BehindDatabaseClock)
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'


def test_legacy_missing_received_time_refuses_without_backfill(observed):
    engine,key=observed
    with engine.begin() as conn:conn.execute(text('UPDATE print_jobs SET observed_received_at=NULL WHERE id=70'))
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='refused'
    assert binding(engine) is None
    with engine.connect() as conn:assert conn.execute(text('SELECT observed_received_at FROM print_jobs WHERE id=70')).scalar_one() is None


def test_business_start_precision_does_not_affect_received_time_binding(observed):
    engine,key=observed
    with engine.begin() as conn:conn.execute(text("UPDATE print_jobs SET started_at='2001-01-01 00:00:00' WHERE id=70"))
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
