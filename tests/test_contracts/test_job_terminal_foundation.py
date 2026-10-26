"""Summary claims roll back with physical truth and never replay per attempt."""
import pytest
from sqlalchemy import text
from core.job_terminal_observations import claim_terminal
from tests.test_contracts.test_private_attempt_valuation_route import private_attempt, creators, review_db

@pytest.mark.parametrize('outcome',['completed','failed','cancelled'])
def test_atomic_attempt_claim_rollback_and_replay(private_attempt,review_db,outcome):
    identifier=private_attempt[3]
    review_db.execute(text('UPDATE print_jobs SET status=:status WHERE id=:id'),{'status':outcome,'id':identifier})
    assert claim_terminal(review_db.connection(),identifier)
    review_db.rollback()
    assert review_db.execute(text('SELECT count(*) FROM job_summary_outbox')).scalar_one()==0
    review_db.execute(text('UPDATE print_jobs SET status=:status WHERE id=:id'),{'status':outcome,'id':identifier})
    assert claim_terminal(review_db.connection(),identifier)
    review_db.commit()
    assert not claim_terminal(review_db.connection(),identifier)
    physical=review_db.execute(text('SELECT economic_uid,economic_birth_nonce FROM print_jobs WHERE id=:id'),{'id':identifier}).one()
    observed=review_db.execute(text('SELECT print_attempt_uid,attempt_birth,outcome FROM job_summary_outbox')).one()
    assert observed==(*physical,outcome)


def test_inspected_business_record_keeps_job_and_history(private_attempt,review_db):
    from fastapi import HTTPException
    from modules.orders.services import assert_job_history_deletable
    from modules.jobs.services import delete_org_jobs
    identifier=private_attempt[3]
    job,uid=review_db.execute(text('SELECT j.id,j.economic_uid FROM print_jobs p JOIN jobs j ON j.id=p.scheduled_job_id WHERE p.id=:id'),{'id':identifier}).one()
    review_db.execute(text("INSERT INTO production_recipes(id,owner,definition) VALUES('retain-recipe',0,'{}')"))
    review_db.execute(text("INSERT INTO production_acceptances(id,owner,job_uid,claim_token,recipe_id,request_hash,definition) VALUES('retain',0,:uid,'claim','retain-recipe','request','{}')"),{'uid':uid})
    review_db.execute(text("INSERT INTO job_timeline_events(id,job_id,kind,occurred_at) VALUES('retain',:job,'completed',CURRENT_TIMESTAMP)"),{'job':job});review_db.commit()
    with pytest.raises(HTTPException) as exc:assert_job_history_deletable(review_db,job)
    assert exc.value.status_code==409
    assert review_db.execute(text("SELECT count(*) FROM job_timeline_events WHERE id='retain'")).scalar_one()==1


@pytest.mark.parametrize('education',[False,True])
def test_only_explicit_education_claim_suppresses_fallback(private_attempt,review_db,monkeypatch,education):
    import json
    from unittest.mock import Mock
    from core import db as core_db
    from core.job_terminal_observations import dispatch_terminal_alert
    identifier=private_attempt[3]
    review_db.execute(text("UPDATE print_jobs SET status='completed' WHERE id=:id"), {'id':identifier})
    assert claim_terminal(review_db.connection(),identifier,deliver_alerts=not education)
    review_db.commit()
    monkeypatch.setattr(core_db,'engine',review_db.get_bind())
    if education:
        state=review_db.execute(text('SELECT delivery_state FROM job_summary_outbox')).scalar_one()
        assert json.loads(state)=={'education_suppressed':True}
    review_db.rollback()
    fallback=Mock()
    dispatch_terminal_alert(identifier,fallback,alert_type='print_complete',severity='info',title='Terminal')
    fallback.assert_not_called()


@pytest.mark.parametrize('education',[False,True])
@pytest.mark.parametrize('outcome',['completed','failed'])
def test_identified_unlinked_claim_preserves_direct_alert(tmp_path,monkeypatch,education,outcome):
    # Isolate the terminal boundary from economic ownership admission: an
    # imported/unlinked identified attempt must never silently lose its alert.
    from sqlalchemy import create_engine
    from unittest.mock import Mock
    from core import db as core_db
    from core.job_terminal_observations import dispatch_terminal_alert
    engine=create_engine('sqlite:///'+str(tmp_path/'unlinked.db'))
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE jobs(id INTEGER PRIMARY KEY,charged_to_org_id INTEGER)')
        c.exec_driver_sql('CREATE TABLE print_jobs(id INTEGER PRIMARY KEY,economic_uid TEXT,economic_birth_nonce TEXT,scheduled_job_id INTEGER,printer_id INTEGER,status TEXT)')
        c.exec_driver_sql("CREATE TABLE job_summary_outbox(print_attempt_uid TEXT PRIMARY KEY,attempt_birth TEXT,job_id INTEGER,printer_id INTEGER,org_id INTEGER,outcome TEXT,delivery_state TEXT DEFAULT '{}')")
        c.execute(text("INSERT INTO print_jobs VALUES(1,'attempt','birth',NULL,7,:status)"),{'status':outcome})
        assert claim_terminal(c,1,deliver_alerts=not education)
    monkeypatch.setattr(core_db,'engine',engine)
    fallback=Mock()
    dispatch_terminal_alert(1,fallback,title='Physical terminal',printer_id=7)
    assert fallback.call_count==int(not education)
    if not education:fallback.assert_called_once_with(title='Physical terminal',printer_id=7)
    engine.dispose()
