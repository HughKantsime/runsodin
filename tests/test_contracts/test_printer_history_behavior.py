"""Printer telemetry/history authorization and actual stored data behavior."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def history_client(token_client,review_db,monkeypatch):
    from modules.printers import routes_status
    client,issue=token_client;client.app.include_router(routes_status.router)
    calls=[]
    monkeypatch.setattr(routes_status,'_fetch_printer_live_status',lambda pid,db:(calls.append(pid) or {'state':'synthetic'}))
    review_db.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,org_id,shared,nozzle_temp) VALUES(199,'Synthetic foreign','Synthetic','Synthetic',1,'bambu',2,0,211)"))
    review_db.execute(text("INSERT INTO printer_telemetry(printer_id,recorded_at,nozzle_temp,bed_temp,fan_speed) VALUES(199,CURRENT_TIMESTAMP,211,61,75)"))
    review_db.execute(text("INSERT INTO hms_error_history(printer_id,code,message,severity,source,occurred_at) VALUES(199,'SYNTHETIC','Synthetic foreign error','warning','mqtt',CURRENT_TIMESTAMP)"))
    review_db.commit()
    return client,issue,calls

@pytest.mark.parametrize('endpoint',['telemetry','hms-history','nozzle-status','live-status'])
def test_foreign_printer_history_and_status_are_not_disclosed(history_client,endpoint):
    client,issue,calls=history_client
    response=client.get(f'/printers/199/{endpoint}',headers=issue(['read:printers']))
    assert response.status_code==404
    assert calls==[]

@pytest.mark.parametrize('endpoint',['telemetry','hms-history','nozzle-status','live-status'])
def test_unrelated_scoped_token_cannot_read_printer_history(history_client,endpoint):
    client,issue,calls=history_client
    response=client.get(f'/printers/199/{endpoint}',headers=issue(['write:jobs']))
    assert response.status_code==403
    assert calls==[]

@pytest.mark.parametrize('endpoint',['telemetry','hms-history','nozzle-status','live-status'])
@pytest.mark.parametrize('role,user_org,printer_org,shared,expected',[
    ('viewer',1,1,False,200),('viewer',1,None,False,200),
    ('viewer',1,2,True,200),('viewer',1,2,False,404),
    ('operator',None,2,False,404),('operator',None,None,False,200),
    ('admin',1,2,False,404),('admin',None,2,False,200),
])
def test_printer_history_resource_visibility(history_client,review_db,endpoint,role,user_org,printer_org,shared,expected):
    client,issue,calls=history_client
    review_db.execute(text('UPDATE users SET role=:role,group_id=:org WHERE id=3'),{'role':role,'org':user_org})
    review_db.execute(text('UPDATE printers SET org_id=:org,shared=:shared WHERE id=199'),{'org':printer_org,'shared':shared})
    review_db.commit()
    response=client.get(f'/printers/199/{endpoint}',headers=issue(['read:printers']))
    assert response.status_code==expected
    assert calls==([199] if endpoint=='live-status' and expected==200 else [])
    if expected==200:
        data=response.json()
        if endpoint=='telemetry':assert len(data)==1 and data[0]['nozzle_temp']==211
        if endpoint=='hms-history':assert data['total']==1 and data['frequency']=={'SYNTHETIC':1}

@pytest.mark.parametrize('endpoint',['telemetry','hms-history','nozzle-status','live-status'])
def test_missing_printer_returns_not_found_before_live_fetch(history_client,endpoint):
    client,issue,calls=history_client
    assert client.get(f'/printers/999999/{endpoint}',headers=issue(['read:printers'])).status_code==404
    assert calls==[]

@pytest.mark.parametrize('endpoint',['telemetry','hms-history','nozzle-status','live-status'])
@pytest.mark.parametrize('credential',['read:printers','read','interactive'])
def test_authorized_viewer_reads_shared_printer_history(history_client,review_db,endpoint,credential):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,issue,calls=history_client
    review_db.execute(text("UPDATE users SET role='viewer' WHERE id=3"))
    review_db.execute(text('UPDATE printers SET shared=1 WHERE id=199'));review_db.commit()
    headers=interactive_headers() if credential=='interactive' else issue([credential])
    response=client.get(f'/printers/199/{endpoint}',headers=headers)
    assert response.status_code==200
    assert calls==([199] if endpoint=='live-status' else [])

@pytest.mark.parametrize('endpoint,param,invalid',[
    ('telemetry','hours',0),('telemetry','hours',169),
    ('hms-history','days',0),('hms-history','days',91),
])
def test_printer_history_rejects_out_of_range_windows(history_client,endpoint,param,invalid):
    client,issue,calls=history_client
    response=client.get(f'/printers/199/{endpoint}?{param}={invalid}',headers=issue(['read:printers']))
    assert response.status_code==422 and calls==[]


def test_telemetry_window_and_chronological_values(history_client,review_db):
    client,issue,_=history_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'))
    for offset,temp in [('-2 hours',195),('-25 hours',140)]:
        review_db.execute(text("INSERT INTO printer_telemetry(printer_id,recorded_at,nozzle_temp,bed_temp,fan_speed) VALUES(199,datetime('now',:offset),:temp,50,40)"),{'offset':offset,'temp':temp})
    review_db.commit()
    headers=issue(['read:printers'])
    data=client.get('/printers/199/telemetry',headers=headers).json()
    assert [r['nozzle_temp'] for r in data]==[195,211]
    assert data[0]['recorded_at']<data[1]['recorded_at']
    assert [r['nozzle_temp'] for r in client.get('/printers/199/telemetry?hours=1',headers=headers).json()]==[211]


def test_hms_history_window_frequency_and_reverse_order(history_client,review_db):
    client,issue,_=history_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'))
    for offset,code in [('-2 days','SYNTHETIC'),('-31 days','OLD')]:
        review_db.execute(text("INSERT INTO hms_error_history(printer_id,code,message,severity,source,occurred_at) VALUES(199,:code,'Synthetic history','warning','mqtt',datetime('now',:offset))"),{'offset':offset,'code':code})
    review_db.commit()
    headers=issue(['read:printers'])
    data=client.get('/printers/199/hms-history',headers=headers).json()
    assert data['total']==2 and data['frequency']=={'SYNTHETIC':2}
    assert data['entries'][0]['occurred_at']>data['entries'][1]['occurred_at']
    assert client.get('/printers/199/hms-history?days=1',headers=headers).json()['total']==1
