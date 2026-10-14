"""Owned HTTP completed-model ranking, independent of monetary rankings."""
from sqlalchemy import text
from tests.test_contracts.test_analytics_financial_behavior import analytics_client, review_db, bom_client

def test_completed_job_counts_rank_unpriced_models_and_filter_foreign_rows(analytics_client,review_db):
    for mid,org,status,copies in [(101,1,'completed',99),(103,None,'completed',1),(103,1,'completed',1),(103,2,'completed',1),(101,1,'pending',1),(102,1,'completed',1)]:
        review_db.execute(text("INSERT INTO jobs(item_name,model_id,status,quantity,charged_to_org_id) VALUES('Owned ranking',:model,:status,:qty,:org)"),{'model':mid,'status':status,'qty':copies,'org':org})
    review_db.execute(text('UPDATE models SET cost_per_item=NULL,build_time_hours=NULL WHERE id=103'))
    review_db.commit()
    response=analytics_client.get('/analytics')
    assert response.status_code==200
    assert response.json()['top_by_jobs']==[{'id':103,'name':'Shared model','job_count':2},{'id':101,'name':'Own model','job_count':1}]
    assert all(row['id']!=102 for row in response.json()['top_by_jobs'])
