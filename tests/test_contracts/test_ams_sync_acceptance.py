"""Actual AMS sync HTTP/DB assertions with only printer acquisition substituted."""
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_ams_environment_behavior import ams_client, own_ams_operator, token_client, review_db

@pytest.fixture
def sync_client(own_ams_operator, review_db, monkeypatch):
    from modules.printers import routes_ams
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu import session, broker_policy
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    client, headers, _ = own_ams_operator
    client.app.include_router(routes_ams.router)
    review_db.execute(text("UPDATE printers SET api_host='127.0.0.1',api_key='synthetic-noncredential' WHERE id=199"))
    review_db.execute(text("UPDATE spools SET rfid_tag='synthetic-own-tag',initial_weight_g=1000 WHERE id=199"));review_db.commit()
    calls=[]
    section=BambuPrintSection.model_validate({'ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','tray_color':'123ABCFF','remain':0,'tag_uid':'synthetic-own-tag'}]}]}})
    monkeypatch.setattr(routes_ams.crypto,'decrypt',lambda _: 'synthetic-serial|synthetic-noncredential')
    monkeypatch.setattr(routes_ams.settings,'spoolman_url',None)
    monkeypatch.setattr(feature_flag,'is_v2_enabled',lambda:True)
    monkeypatch.setattr(broker_policy,'resolve_bambu_broker_config',lambda *args,**kwargs:SimpleNamespace(host='127.0.0.1',port=1883,use_tls=False))
    monkeypatch.setattr(session,'read_status_once',lambda *args,**kwargs:(calls.append('acquire') or SimpleNamespace(success=True,section=section)))
    return client,headers,calls


@pytest.mark.parametrize('shared', [False,True])
def test_foreign_printer_sync_cannot_acquire_or_mutate(sync_client, review_db, shared):
    client,headers,calls=sync_client
    review_db.execute(text('UPDATE printers SET org_id=2,shared=:shared WHERE id=199'),{'shared':shared});review_db.commit()
    response=client.post('/printers/199/sync-ams',headers=headers)
    assert response.status_code==404
    assert calls==[]
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500


def test_matching_rfid_zero_remaining_updates_existing_spool(sync_client, review_db):
    client,headers,calls=sync_client
    response=client.post('/printers/199/sync-ams',headers=headers)
    assert response.status_code==200 and calls==['acquire']
    assert response.json()['slots'][0]['matched']=='rfid'
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==0


def test_matching_foreign_rfid_never_moves_or_reveals_foreign_spool(sync_client, review_db):
    client,headers,calls=sync_client
    review_db.execute(text('UPDATE spools SET org_id=2 WHERE id=199'));review_db.commit()
    response=client.post('/printers/199/sync-ams',headers=headers)
    assert response.status_code==409
    assert '199' not in response.text and 'synthetic-own-tag' not in response.text
    assert tuple(review_db.execute(text('SELECT location_printer_id,location_slot,remaining_weight_g FROM spools WHERE id=199')).one())==(None,None,500)


@pytest.mark.parametrize('scope',['read:printers','write:jobs'])
def test_sync_requires_printer_write_scope(sync_client, ams_client, scope):
    client,_,calls=sync_client
    _,issue,_=ams_client
    response=client.post('/printers/199/sync-ams',headers=issue([scope]))
    assert response.status_code==403 and calls==[]


def test_poisoned_assigned_foreign_spool_refused_before_acquisition(sync_client, review_db):
    client,headers,calls=sync_client
    review_db.execute(text('UPDATE spools SET org_id=2 WHERE id=199'))
    review_db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE id=199'));review_db.commit()
    response=client.post('/printers/199/sync-ams',headers=headers)
    assert response.status_code==409 and calls==[]
    assert review_db.execute(text('SELECT color FROM filament_slots WHERE id=199')).scalar_one()=='#000000'


def test_owned_spool_with_foreign_previous_location_refused(sync_client, review_db):
    client,headers,calls=sync_client
    review_db.execute(text("INSERT INTO printers(id,name,api_type,org_id) VALUES(200,'Synthetic prior foreign','bambu',2)"))
    review_db.execute(text('UPDATE spools SET location_printer_id=200 WHERE id=199'));review_db.commit()
    response=client.post('/printers/199/sync-ams',headers=headers)
    assert response.status_code==409
    assert review_db.execute(text('SELECT location_printer_id FROM spools WHERE id=199')).scalar_one()==200


@pytest.mark.parametrize('remaining', [None,-1,101])
def test_unknown_existing_rfid_preserves_retained_weight(sync_client, review_db, monkeypatch, remaining):
    from modules.printers.telemetry.bambu import session
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    client,headers,_=sync_client
    section=BambuPrintSection.model_validate({'ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','remain':remaining,'tag_uid':'synthetic-own-tag'}]}]}})
    monkeypatch.setattr(session,'read_status_once',lambda *args,**kwargs:SimpleNamespace(success=True,section=section))
    assert client.post('/printers/199/sync-ams',headers=headers).status_code==200
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500


@pytest.mark.parametrize('remaining,expected', [(0,0),(None,None),(-1,None),(50,500)])
def test_new_rfid_spool_inherits_printer_org_and_preserves_measurement(sync_client, review_db, monkeypatch, remaining, expected):
    from modules.printers.telemetry.bambu import session
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    client,headers,_=sync_client
    section=BambuPrintSection.model_validate({'ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','remain':remaining,'tag_uid':'synthetic-new-tag'}]}]}})
    monkeypatch.setattr(session,'read_status_once',lambda *args,**kwargs:SimpleNamespace(success=True,section=section))
    response=client.post('/printers/199/sync-ams',headers=headers)
    assert response.status_code==200
    row=review_db.execute(text("SELECT org_id,remaining_weight_g FROM spools WHERE rfid_tag='synthetic-new-tag'")).one()
    assert tuple(row)==(1,expected)



def test_later_foreign_rfid_refuses_whole_batch_before_earlier_mutations(sync_client, review_db, monkeypatch):
    from modules.printers.telemetry.bambu import session
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    client,headers,_=sync_client
    review_db.execute(text("INSERT INTO spools(id,filament_id,rfid_tag,org_id,remaining_weight_g) VALUES(200,199,'synthetic-foreign-tag',2,300)"));review_db.commit()
    section=BambuPrintSection.model_validate({'ams':{'ams':[{'id':'0','tray':[
        {'id':'0','tray_type':'PLA','remain':0,'tag_uid':'synthetic-own-tag'},
        {'id':'1','tray_type':'PETG','remain':50,'tag_uid':'synthetic-foreign-tag'},
    ]}]}})
    monkeypatch.setattr(session,'read_status_once',lambda *args,**kwargs:SimpleNamespace(success=True,section=section))
    response=client.post('/printers/199/sync-ams',headers=headers)
    assert response.status_code==409
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
    assert review_db.execute(text('SELECT COUNT(*) FROM filament_slots WHERE printer_id=199')).scalar_one()==1
    assert review_db.execute(text('SELECT color FROM filament_slots WHERE id=199')).scalar_one()=='#000000'


def test_unscoped_existing_spool_preserves_existing_access_policy(sync_client, review_db):
    client,headers,_=sync_client
    review_db.execute(text('UPDATE spools SET org_id=NULL WHERE id=199'));review_db.commit()
    assert client.post('/printers/199/sync-ams',headers=headers).status_code==200
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==0


@pytest.mark.parametrize('resource',['printer','spool','previous_printer'])
def test_ownership_change_during_acquisition_refuses_all_mutations(sync_client, review_db, monkeypatch, resource):
    from sqlalchemy.orm import Session
    from modules.printers.telemetry.bambu import session
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    client,headers,_=sync_client
    if resource=='previous_printer':
        review_db.execute(text("INSERT INTO printers(id,name,org_id,api_type) VALUES(200,'Synthetic previous','bambu',1)"))
        review_db.execute(text('UPDATE spools SET location_printer_id=200 WHERE id=199'));review_db.commit()
    section=BambuPrintSection.model_validate({'ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','remain':0,'tag_uid':'synthetic-own-tag'}]}]}})
    def acquire(*args,**kwargs):
        with Session(review_db.get_bind()) as concurrent:
            if resource=='spool':
                concurrent.execute(text('UPDATE spools SET org_id=2 WHERE id=199'))
            else:
                concurrent.execute(text('UPDATE printers SET org_id=2 WHERE id=:id'),{'id':199 if resource=='printer' else 200})
            concurrent.commit()
        return SimpleNamespace(success=True,section=section)
    monkeypatch.setattr(session,'read_status_once',acquire)
    response=client.post('/printers/199/sync-ams',headers=headers)
    assert response.status_code in (404,409)
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
    assert review_db.execute(text('SELECT color FROM filament_slots WHERE id=199')).scalar_one()=='#000000'



def test_writer_contention_rolls_back_without_mutations_then_retry_succeeds(sync_client, review_db, monkeypatch):
    from modules.printers.telemetry.bambu import session
    client,headers,_=sync_client
    original=session.read_status_once
    held=[]
    def acquire(*args,**kwargs):
        if not held:
            connection=review_db.get_bind().connect()
            transaction=connection.begin()
            connection.execute(text('UPDATE printers SET name=name WHERE id=199'))
            held.append((connection,transaction))
        return original(*args,**kwargs)
    monkeypatch.setattr(session,'read_status_once',acquire)
    try:
        response=client.post('/printers/199/sync-ams',headers=headers)
        assert response.status_code==409
        assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
        assert review_db.execute(text('SELECT color FROM filament_slots WHERE id=199')).scalar_one()=='#000000'
    finally:
        if held:
            held[0][1].rollback();held[0][0].close()
    assert client.post('/printers/199/sync-ams',headers=headers).status_code==200


def test_new_foreign_rfid_after_graph_validation_is_not_an_authorized_target(sync_client, review_db, monkeypatch):
    from modules.printers import routes_ams
    from modules.printers.telemetry.bambu import session
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    client,headers,_=sync_client
    section=BambuPrintSection.model_validate({'ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','remain':50,'tag_uid':'synthetic-late-tag'}]}]}})
    monkeypatch.setattr(session,'read_status_once',lambda *args,**kwargs:SimpleNamespace(success=True,section=section))
    original=routes_ams._lock_ams_mutation
    def late_match(db,*args,**kwargs):
        result=original(db,*args,**kwargs)
        # Same-transaction injection simulates a newly visible match. Native PG
        # concurrent insertion remains a separate acceptance requirement.
        db.execute(text("INSERT INTO spools(id,filament_id,rfid_tag,org_id,remaining_weight_g) VALUES(200,199,'synthetic-late-tag',2,300)"))
        return result
    monkeypatch.setattr(routes_ams,'_lock_ams_mutation',late_match)
    response=client.post('/printers/199/sync-ams',headers=headers)
    assert response.status_code==409
    assert 'synthetic-late-tag' not in response.text
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
    assert review_db.execute(text('SELECT color FROM filament_slots WHERE id=199')).scalar_one()=='#000000'


@pytest.mark.parametrize('initial', [-100,float('inf'),float('-inf')])
def test_invalid_retained_initial_weight_cannot_create_invalid_inventory_measurement(sync_client, review_db, monkeypatch, initial):
    from modules.printers.telemetry.bambu import session
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    client,headers,_=sync_client
    review_db.execute(text('UPDATE spools SET initial_weight_g=:initial WHERE id=199'),{'initial':initial});review_db.commit()
    section=BambuPrintSection.model_validate({'ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','remain':50,'tag_uid':'synthetic-own-tag'}]}]}})
    monkeypatch.setattr(session,'read_status_once',lambda *args,**kwargs:SimpleNamespace(success=True,section=section))
    assert client.post('/printers/199/sync-ams',headers=headers).status_code==200
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
