"""Advertised creation-time quota enforcement on actual isolated job routes."""
import pytest
from sqlalchemy import text
from core.quota import _get_period_key
from tests.test_contracts.test_job_approval_notification_behavior import approval_client,review_db,signed_license

@pytest.mark.parametrize('field,limit,usage_column,used',[
    ('quota_jobs',1,'jobs_used',1),('quota_grams',10,'grams_used',100),('quota_hours',1,'hours_used',2),
])
def test_exhausted_user_quota_blocks_new_job_without_writes(approval_client,review_db,field,limit,usage_column,used):
    client,user=approval_client
    user.update({field:limit,'quota_period':'monthly'})
    review_db.execute(text('INSERT INTO quota_usage(user_id,period_key,'+usage_column+') VALUES(1,:period,:used)'),{'period':_get_period_key('monthly'),'used':used});review_db.commit()
    before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
    response=client.post('/api/v1/jobs',json={'item_name':'Synthetic exhausted quota'})
    assert response.status_code==429
    assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before


def test_job_count_increment_blocks_second_submission(approval_client,review_db):
    client,user=approval_client;user.update(quota_jobs=1,quota_period='monthly')
    assert client.post('/api/v1/jobs',json={'item_name':'Synthetic quota first'}).status_code==201
    assert client.post('/api/v1/jobs',json={'item_name':'Synthetic quota second'}).status_code==429
    assert review_db.execute(text('SELECT jobs_used FROM quota_usage WHERE user_id=1')).scalar_one()==1
