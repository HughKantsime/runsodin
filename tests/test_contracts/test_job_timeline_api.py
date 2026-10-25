"""Job history uses the job's read audience and chronological ordering."""
from sqlalchemy import text
from tests.test_contracts.test_job_dispatch_status import status_client, token_client, observed, review_db


def test_timeline_job_read_access_and_order(status_client,review_db):
    client,headers=status_client
    review_db.execute(text("INSERT INTO job_timeline_events(id,job_id,kind,occurred_at) VALUES('later',53,'completed','2026-10-25 12:01:00'),('earlier',53,'started','2026-10-25 12:00:00')"));review_db.commit()
    response=client.get('/jobs/53/timeline',headers=headers)
    assert response.status_code==200,response.text
    assert [v['kind'] for v in response.json()['events']]==['started','completed']
    assert all(v['detail']=={} for v in response.json()['events'])
    review_db.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id=53'));review_db.commit()
    assert client.get('/jobs/53/timeline',headers=headers).status_code==404
    assert client.get('/jobs/53/timeline').status_code==401


def test_timeline_read_scope_matches_job_reader(status_client,token_client):
    client,_=status_client
    _,issue=token_client
    assert client.get('/jobs/53/timeline',headers=issue(['read:jobs'])).status_code==200
    assert client.get('/jobs/53/timeline',headers=issue(['read:spools'])).status_code==403


def test_actual_user_erasure_anonymizes_timeline_actor(review_db):
    import asyncio
    from modules.organizations.routes_sessions import erase_user_data
    review_db.execute(text("INSERT INTO users(id,username,password_hash,role,group_id,is_active) VALUES(441,'timeline-actor','unused','operator',1,TRUE)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id) VALUES(441,'timeline','completed',1)"))
    review_db.execute(text("INSERT INTO job_timeline_events(id,job_id,kind,actor_user_id,occurred_at) VALUES('erase',441,'bed_clear_confirmed',441,CURRENT_TIMESTAMP)"));review_db.commit()
    asyncio.run(erase_user_data(441,{'id':3,'role':'admin','group_id':1},review_db))
    assert review_db.execute(text("SELECT actor_user_id,actor_token_id FROM job_timeline_events WHERE id='erase'")).one()==(None,None)
