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
