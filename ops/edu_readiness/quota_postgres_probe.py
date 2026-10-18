"""Owned PostgreSQL probe of current quota source, without hardware or production data."""
import sys,json
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from core.quota import charge_job_quota,_get_period_key
from core.errors import OdinError
engine=create_engine('postgresql+psycopg://postgres@'+sys.argv[1]+'/postgres')
with engine.begin() as conn:
    conn.execute(text('CREATE TABLE quota_usage(id SERIAL PRIMARY KEY,user_id INTEGER NOT NULL,period_key VARCHAR(20) NOT NULL,grams_used REAL DEFAULT 0,hours_used REAL DEFAULT 0,jobs_used INTEGER DEFAULT 0,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,UNIQUE(user_id,period_key))'))
    conn.execute(text('CREATE TABLE jobs(id SERIAL PRIMARY KEY,item_name TEXT NOT NULL)'))
actor={'id':1,'quota_jobs':1,'quota_period':'monthly'}
checks=[]
for existing in [False,True]:
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM quota_usage'));conn.execute(text('DELETE FROM jobs'))
        if existing:conn.execute(text('INSERT INTO quota_usage(user_id,period_key) VALUES(1,:pk)'),{'pk':_get_period_key('monthly')})
    barrier=Barrier(2)
    def create():
        with Session(engine) as db:
            barrier.wait()
            try:
                charge_job_quota(db,actor)
                db.execute(text("INSERT INTO jobs(item_name) VALUES('owned')"));db.commit()
                return 'accepted'
            except OdinError as exc:
                db.rollback();assert exc.status==429
                return 'denied'
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:create(),range(2)))
    assert sorted(results)==['accepted','denied'],results
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT jobs_used FROM quota_usage'))==1
        assert conn.scalar(text('SELECT count(*) FROM jobs'))==1
    checks.append('one_allowance_existing_'+str(existing))
with engine.begin() as conn:conn.execute(text('DELETE FROM quota_usage'));conn.execute(text('DELETE FROM jobs'))
with Session(engine) as db:
    charge_job_quota(db,actor)
    db.execute(text("INSERT INTO jobs(item_name) VALUES('rollback')"));db.rollback()
with engine.connect() as conn:
    assert conn.scalar(text('SELECT count(*) FROM quota_usage'))==0
    assert conn.scalar(text('SELECT count(*) FROM jobs'))==0
checks.append('job_quota_rollback')
for kind in ['grams','hours']:
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM quota_usage'))
        conn.execute(text(f'INSERT INTO quota_usage(user_id,period_key,{kind}_used) VALUES(1,:pk,5)'),{'pk':_get_period_key('monthly')})  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- kind iterates a literal grams/hours list in this probe script
    with Session(engine) as db:
        try:charge_job_quota(db,{'id':1,'quota_'+kind:5})
        except OdinError as exc:assert exc.status==429;db.rollback()
        else:raise AssertionError('Exhaustion admitted')
    checks.append(kind+'_exhaustion')
print(json.dumps({'passed':True,'checks':checks,'dialect':engine.dialect.name}))
