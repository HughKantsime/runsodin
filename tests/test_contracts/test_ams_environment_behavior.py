"""Actual AMS HTTP/database isolation and command-boundary acceptance."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db

@pytest.fixture
def ams_client(token_client, review_db, monkeypatch):
    from modules.printers import routes_ams_env, route_utils
    client, issue = token_client
    client.app.include_router(routes_ams_env.router)
    calls = []
    monkeypatch.setattr(route_utils, '_bambu_command_direct', lambda *args: (calls.append(args[1:]) or True))
    review_db.execute(text("CREATE TABLE IF NOT EXISTS ams_telemetry(id INTEGER PRIMARY KEY,printer_id INTEGER,ams_unit INTEGER,humidity REAL,temperature REAL,recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP)"))
    review_db.execute(text("INSERT INTO printers(id,name,api_type,org_id,shared) VALUES(199,'Synthetic foreign AMS','bambu',2,0)"))
    review_db.execute(text("INSERT INTO ams_telemetry(printer_id,ams_unit,humidity,temperature) VALUES(199,0,3,24)"))
    review_db.execute(text("INSERT INTO filament_slots(id,printer_id,slot_number,filament_type,color) VALUES(199,199,0,'PLA','#000000')"))
    review_db.commit()
    return client, issue, calls

@pytest.mark.parametrize('endpoint', ['environment', 'current'])
def test_foreign_ams_read_is_hidden(ams_client, endpoint):
    client, issue, _ = ams_client
    assert client.get(f'/printers/199/ams/{endpoint}', headers=issue(['read:printers'])).status_code == 404

@pytest.mark.parametrize('endpoint', ['environment', 'current'])
def test_unrelated_scope_cannot_read_ams(ams_client, endpoint):
    client, issue, _ = ams_client
    assert client.get(f'/printers/199/ams/{endpoint}', headers=issue(['write:jobs'])).status_code == 403

@pytest.mark.parametrize('endpoint', ['environment', 'current'])
def test_missing_printer_is_not_an_empty_ams_success(ams_client, endpoint):
    client, issue, _ = ams_client
    assert client.get(f'/printers/9191/ams/{endpoint}', headers=issue(['read:printers'])).status_code == 404

@pytest.mark.parametrize('action', ['refresh', 'configure'])
def test_foreign_ams_commands_do_not_reach_transport(ams_client, review_db, action):
    client, issue, calls = ams_client
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3")); review_db.commit()
    headers = issue(['write:printers'])
    if action == 'refresh':
        result = client.post('/printers/199/ams/refresh', headers=headers)
    else:
        result = client.put('/printers/199/ams/0/slots/0', headers=headers, json={'material':'PLA','color':'#FFFFFF'})
    assert result.status_code == 404
    assert calls == []
    assert tuple(review_db.execute(text('SELECT filament_type,color,assigned_spool_id FROM filament_slots WHERE id=199')).one()) == ('PLA','#000000',None)

@pytest.mark.parametrize('endpoint', ['environment', 'current'])
@pytest.mark.parametrize('shared,expected', [(0,404),(1,200)])
def test_shared_foreign_ams_read_policy(ams_client, review_db, endpoint, shared, expected):
    client, issue, _ = ams_client
    review_db.execute(text('UPDATE printers SET shared=:shared WHERE id=199'), {'shared':shared}); review_db.commit()
    assert client.get(f'/printers/199/ams/{endpoint}', headers=issue(['read:printers'])).status_code == expected

@pytest.mark.parametrize('action', ['refresh', 'configure'])
def test_shared_foreign_ams_still_cannot_be_commanded(ams_client, review_db, action):
    client, issue, calls = ams_client
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"))
    review_db.execute(text('UPDATE printers SET shared=1 WHERE id=199')); review_db.commit()
    headers = issue(['write:printers'])
    response = client.post('/printers/199/ams/refresh', headers=headers) if action == 'refresh' else client.put('/printers/199/ams/0/slots/0', headers=headers, json={})
    assert response.status_code == 404 and calls == []
    assert tuple(review_db.execute(text('SELECT filament_type,color,assigned_spool_id FROM filament_slots WHERE id=199')).one()) == ('PLA','#000000',None)

@pytest.mark.parametrize('endpoint', ['environment', 'current'])
def test_owned_ams_read_preserves_actual_measurements(ams_client, review_db, endpoint):
    client, issue, _ = ams_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199')); review_db.commit()
    response = client.get(f'/printers/199/ams/{endpoint}', headers=issue(['read:printers']))
    assert response.status_code == 200
    payload = response.json()
    record = payload['units']['0'][0] if endpoint == 'environment' else payload['units'][0]
    assert record['humidity'] == 3 and record['temperature'] == 24

@pytest.mark.parametrize('action', ['refresh', 'configure'])
@pytest.mark.parametrize('role,scope', [('viewer','write:printers'),('operator','write:jobs')])
def test_ams_command_role_and_scope_denial_preserves_slots(ams_client, review_db, action, role, scope):
    client, issue, calls = ams_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'))
    review_db.execute(text('UPDATE users SET role=:role WHERE id=3'), {'role':role}); review_db.commit()
    response = client.post('/printers/199/ams/refresh', headers=issue([scope])) if action == 'refresh' else client.put('/printers/199/ams/0/slots/0', headers=issue([scope]), json={})
    assert response.status_code == 403 and calls == []
    assert tuple(review_db.execute(text('SELECT filament_type,color,assigned_spool_id FROM filament_slots WHERE id=199')).one()) == ('PLA','#000000',None)

def test_current_ams_has_one_latest_reading_per_unit_on_timestamp_tie(ams_client, review_db):
    client, issue, _ = ams_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'))
    review_db.execute(text("UPDATE ams_telemetry SET recorded_at=CURRENT_TIMESTAMP WHERE printer_id=199"))
    review_db.execute(text("INSERT INTO ams_telemetry(printer_id,ams_unit,humidity,temperature,recorded_at) SELECT 199,0,4,26,recorded_at FROM ams_telemetry WHERE printer_id=199 LIMIT 1")); review_db.commit()
    result = client.get('/printers/199/ams/current', headers=issue(['read:printers']))
    assert result.status_code == 200
    assert len(result.json()['units']) == 1
    assert result.json()['units'][0]['temperature'] == 26

def test_configure_ams_command_failure_is_not_success(ams_client, review_db, monkeypatch):
    from modules.printers import route_utils
    client, issue, _ = ams_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'))
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3")); review_db.commit()
    monkeypatch.setattr(route_utils, '_bambu_command_direct', lambda *args: False)
    result = client.put('/printers/199/ams/0/slots/0', headers=issue(['write:printers']), json={})
    assert result.status_code == 503
    assert tuple(review_db.execute(text('SELECT filament_type,color,assigned_spool_id FROM filament_slots WHERE id=199')).one()) == ('PLA','#000000',None)

def test_missing_spool_rejected_before_ams_command(ams_client, review_db):
    client, issue, calls = ams_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'))
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3")); review_db.commit()
    result = client.put('/printers/199/ams/0/slots/0', headers=issue(['write:printers']), json={'spool_id':9191})
    assert result.status_code == 404
    assert calls == []
    assert tuple(review_db.execute(text('SELECT filament_type,color,assigned_spool_id FROM filament_slots WHERE id=199')).one()) == ('PLA','#000000',None)

@pytest.fixture
def own_ams_operator(ams_client, review_db):
    client, issue, calls = ams_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'))
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"))
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(199,'Synthetic','AMS acceptance','PLA')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,status,org_id,remaining_weight_g) VALUES(199,199,'active',1,500)"))
    review_db.commit()
    return client, issue(['write:printers']), calls

def test_successful_ams_configuration_persists_only_after_command(own_ams_operator, review_db):
    client, headers, calls = own_ams_operator
    response = client.put('/printers/199/ams/0/slots/0', headers=headers, json={'material':'PETG','color':'#FF5500','k_factor':0.028,'spool_id':199})
    assert response.status_code == 200
    assert calls == [('set_ams_filament',0,0,'PETG','#FF5500',0.028)]
    assert tuple(review_db.execute(text('SELECT filament_type,color,assigned_spool_id FROM filament_slots WHERE id=199')).one()) == ('PETG','#FF5500',199)

@pytest.mark.parametrize('spool_org', [2])
def test_inaccessible_spool_cannot_be_assigned(own_ams_operator, review_db, spool_org):
    client, headers, calls = own_ams_operator
    review_db.execute(text('UPDATE spools SET org_id=:org WHERE id=199'), {'org':spool_org}); review_db.commit()
    response = client.put('/printers/199/ams/0/slots/0', headers=headers, json={'spool_id':199})
    assert response.status_code == 404 and calls == []
    assert review_db.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE id=199')).scalar_one() is None

def test_missing_assignment_slot_is_rejected_before_command(own_ams_operator, review_db):
    client, headers, calls = own_ams_operator
    response = client.put('/printers/199/ams/0/slots/1', headers=headers, json={'spool_id':199})
    assert response.status_code == 404 and calls == []

def test_failed_assignment_command_preserves_slot_and_spool(own_ams_operator, review_db, monkeypatch):
    from modules.printers import route_utils
    client, headers, _ = own_ams_operator
    monkeypatch.setattr(route_utils, '_bambu_command_direct', lambda *args: False)
    response = client.put('/printers/199/ams/0/slots/0', headers=headers, json={'material':'PETG','color':'#FF5500','spool_id':199})
    assert response.status_code == 503
    assert tuple(review_db.execute(text('SELECT filament_type,color,assigned_spool_id FROM filament_slots WHERE id=199')).one()) == ('PLA','#000000',None)
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one() == 500

def test_unscoped_spool_remains_assignable_under_existing_policy(own_ams_operator, review_db):
    client, headers, calls = own_ams_operator
    review_db.execute(text('UPDATE spools SET org_id=NULL WHERE id=199')); review_db.commit()
    response = client.put('/printers/199/ams/0/slots/0', headers=headers, json={'spool_id':199})
    assert response.status_code == 200 and len(calls) == 1
    assert review_db.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE id=199')).scalar_one() == 199

@pytest.mark.parametrize('protocol,expected', [('bambu','level'),('moonraker','unknown'),(None,'unknown')])
def test_ams_humidity_units_are_explicit_not_guessed_from_numbers(ams_client, review_db, protocol, expected):
    client, issue, _ = ams_client
    review_db.execute(text('UPDATE printers SET org_id=1,api_type=:protocol WHERE id=199'), {'protocol':protocol}); review_db.commit()
    response = client.get('/printers/199/ams/environment', headers=issue(['read:printers']))
    assert response.status_code == 200
    assert response.json()['humidity_unit'] == expected
    assert response.json()['units']['0'][0]['humidity'] == 3
