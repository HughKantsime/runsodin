"""Exact encrypted JSON CAS against both SQL dialects; no live account required."""
import os
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from core.models import SystemConfig
from modules.orders import square_store

@pytest.fixture(params=['sqlite','postgresql'])
def config_engine(request,tmp_path,monkeypatch):
    from core.schema.bootstrap import import_all_models
    import_all_models()
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    if request.param=='postgresql':
        url=os.environ.get('SQUARE_TEST_POSTGRES_URL')
        if not url:pytest.skip('Disposable PostgreSQL URL not supplied')
    else:url='sqlite:///'+str(tmp_path/'config.db')
    engine=create_engine(url)
    SystemConfig.__table__.create(engine,checkfirst=True)
    yield engine
    SystemConfig.__table__.drop(engine)
    engine.dispose()

def original():return {'version':1,'tenant':0,'token':'synthetic','environment':'sandbox','merchant_id':'M1','mappings':{}}

def test_stale_ciphertext_update_cannot_overwrite_newer_connection(config_engine):
    with Session(config_engine) as db:square_store.save(db,0,original(),None);db.commit()
    with Session(config_engine) as first,Session(config_engine) as second:
        a,old_a=square_store.load(first,0);b,old_b=square_store.load(second,0)
        a['merchant_id']='NEW';square_store.save(first,0,a,old_a);first.commit()
        b['merchant_id']='STALE'
        with pytest.raises(HTTPException) as error:square_store.save(second,0,b,old_b)
        assert error.value.status_code==409;second.rollback()
    with Session(config_engine) as db:assert square_store.load(db,0)[0]['merchant_id']=='NEW'

def test_duplicate_first_insert_is_conflict_not_credential_overwrite(config_engine):
    with Session(config_engine) as db:square_store.save(db,0,original(),None);db.commit()
    with Session(config_engine) as db:
        with pytest.raises(HTTPException) as error:square_store.save(db,0,original(),None)
        assert error.value.status_code==409
        assert square_store.load(db,0)[0]['merchant_id']=='M1'

@pytest.mark.parametrize('initial',[True,False])
def test_simultaneous_writers_have_one_winner(config_engine,initial):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    if not initial:
        with Session(config_engine) as db:square_store.save(db,0,original(),None);db.commit()
    barrier=Barrier(2)
    def writer(name):
        with Session(config_engine) as db:
            if initial:doc,previous=original(),None
            else:doc,previous=square_store.load(db,0)
            doc['merchant_id']=name
            barrier.wait(timeout=5)
            try:
                square_store.save(db,0,doc,previous);db.commit();return 'saved'
            except HTTPException as error:
                db.rollback();return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(writer,'A');b=pool.submit(writer,'B')
        assert sorted([str(a.result(timeout=10)),str(b.result(timeout=10))])==['409','saved']
