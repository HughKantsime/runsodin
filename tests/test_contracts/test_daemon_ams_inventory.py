"""Actual monitor callback and owned SQLite inventory; outbound paths substituted."""
from contextlib import contextmanager
from types import SimpleNamespace
import sqlite3
import pytest
from sqlalchemy import text
from tests.test_contracts.test_ams_sync_acceptance import sync_client, own_ams_operator, ams_client, token_client, review_db

@pytest.fixture
def daemon_ams(sync_client, review_db, monkeypatch):
    from modules.printers.monitors import mqtt_printer as module
    path=review_db.get_bind().url.database
    review_db.execute(text('UPDATE filament_slots SET slot_number=1 WHERE id=199'));review_db.commit()
    @contextmanager
    def database():
        connection=sqlite3.connect(path)
        try:yield connection
        finally:connection.close()
    monkeypatch.setattr(module,'get_db',database)
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:1000.0))
    monkeypatch.setattr(module,'mqtt_republish',None)
    events=[]
    monkeypatch.setattr(module,'ws_push',lambda *args,**kwargs:events.append((args,kwargs)))
    monitor=module.PrinterMonitor(199,'Synthetic daemon','127.0.0.1','synthetic-serial','')
    monitor._camera_discovered=True
    for method in ['_on_state_change','_check_spool_levels','_update_progress']:
        monkeypatch.setattr(monitor,method,lambda *args,**kwargs:None)
    def send(remaining=0, trays=None, units=None):
        packet={'gcode_state':'IDLE','bed_temper':60,'nozzle_temper':210,'ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','tray_color':'123ABCFF','tag_uid':'synthetic-own-tag','remain':remaining}]}]}}
        if trays is not None:packet['ams']['ams'][0]['tray']=trays
        if units is not None:packet['ams']['ams']=units
        monitor._on_status(SimpleNamespace(raw_data={'print':packet}))
    return send,monitor,events


def test_daemon_existing_rfid_measured_zero_updates_weight(daemon_ams, review_db):
    send,_,_=daemon_ams
    send(0)
    assert review_db.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE id=199')).scalar_one()==199
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==0


def test_daemon_foreign_rfid_cannot_be_assigned_or_moved(daemon_ams, review_db):
    send,_,_=daemon_ams
    review_db.execute(text('UPDATE spools SET org_id=2 WHERE id=199'));review_db.commit()
    send(50)
    assert review_db.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE id=199')).scalar_one() is None
    assert tuple(review_db.execute(text('SELECT location_printer_id,remaining_weight_g FROM spools WHERE id=199')).one())==(None,500)


def test_daemon_malformed_remaining_does_not_abort_slot_sync(daemon_ams, review_db):
    send,_,_=daemon_ams
    send('bad')
    assert review_db.execute(text('SELECT color_hex FROM filament_slots WHERE id=199')).scalar_one()=='123ABC'
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500


@pytest.mark.parametrize('remaining', [None,'bad',True,12.5,-1,101])
def test_daemon_unknown_remaining_preserves_inventory_and_omits_cache_measurement(daemon_ams, review_db, remaining):
    send,monitor,_=daemon_ams
    send(remaining)
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
    assert 1 not in monitor._state['_ams_remain']


def test_daemon_first_detection_creates_slot_and_matches_authorized_rfid(daemon_ams, review_db):
    send,_,_=daemon_ams
    review_db.execute(text('DELETE FROM filament_slots WHERE id=199'));review_db.commit()
    send(25)
    assert tuple(review_db.execute(text('SELECT assigned_spool_id,color_hex FROM filament_slots WHERE printer_id=199')).one())==(199,'123ABC')
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==250


def test_daemon_existing_assignment_continues_weight_updates(daemon_ams, review_db):
    send,_,_=daemon_ams
    review_db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE id=199'));review_db.commit()
    send(25)
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==250


@pytest.mark.parametrize('failure',['insert','update','commit'])
def test_daemon_batch_failure_rolls_back_inventory_and_next_observation_retries(daemon_ams, review_db, monkeypatch, caplog, failure):
    from modules.printers.monitors import mqtt_printer as module
    send,monitor,_=daemon_ams
    if failure=='insert':
        review_db.execute(text('DELETE FROM filament_slots WHERE id=199'));review_db.commit()
    original=module.get_db
    fired=[False]
    class FaultConnection:
        def __init__(self,connection):self.connection=connection;self.dirty=False
        def __getattr__(self,name):return getattr(self.connection,name)
        def execute(self,statement,parameters=()):
            relevant=statement.startswith('INSERT INTO filament_slots') if failure=='insert' else statement.startswith('UPDATE spools')
            if failure!='commit' and relevant and not fired[0]:
                fired[0]=True;raise RuntimeError('synthetic-private-failure-marker')
            if statement.startswith(('UPDATE filament_slots','INSERT INTO filament_slots')):self.dirty=True
            return self.connection.execute(statement,parameters)
        def commit(self):
            if failure=='commit' and self.dirty and not fired[0]:
                fired[0]=True;raise RuntimeError('synthetic-private-failure-marker')
            self.connection.commit();self.dirty=False
        def rollback(self):self.connection.rollback();self.dirty=False
    @contextmanager
    def faulty_database():
        with original() as connection:yield FaultConnection(connection)
    monkeypatch.setattr(module,'get_db',faulty_database)
    send(0)
    assert fired[0]
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
    assert getattr(monitor,'_last_ams_slot_sync',0)==0
    if failure=='insert':
        assert review_db.execute(text('SELECT COUNT(*) FROM filament_slots WHERE printer_id=199')).scalar_one()==0
    else:
        assert review_db.execute(text('SELECT color_hex FROM filament_slots WHERE id=199')).scalar_one() is None
    assert 'synthetic-private-failure-marker' not in caplog.text
    # Earlier independent heartbeat persistence survives the AMS rollback.
    assert review_db.execute(text('SELECT last_seen FROM printers WHERE id=199')).scalar_one() is not None
    # The outer heartbeat gate admits DB work every ten seconds.
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:1011.0))
    send(0)
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==0
    assert monitor._last_ams_slot_sync==1011



def test_daemon_mixed_foreign_batch_refused_before_own_slot_mutation(daemon_ams, review_db):
    send,monitor,_=daemon_ams
    review_db.execute(text("INSERT INTO spools(id,filament_id,org_id,rfid_tag,remaining_weight_g) VALUES(200,199,2,'synthetic-foreign-tag',300)"));review_db.commit()
    send(trays=[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-own-tag','remain':0}, {'id':'1','tray_type':'PETG','tag_uid':'synthetic-foreign-tag','remain':50}])
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
    assert review_db.execute(text('SELECT color_hex FROM filament_slots WHERE id=199')).scalar_one() is None
    assert review_db.execute(text('SELECT COUNT(*) FROM filament_slots WHERE printer_id=199')).scalar_one()==1
    assert getattr(monitor,'_last_ams_slot_sync',0)==0


def test_daemon_poisoned_foreign_assignment_is_not_mutated(daemon_ams, review_db):
    send,_,_=daemon_ams
    review_db.execute(text('UPDATE spools SET org_id=2 WHERE id=199'))
    review_db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE id=199'));review_db.commit()
    send(50)
    assert review_db.execute(text('SELECT color_hex FROM filament_slots WHERE id=199')).scalar_one() is None
    assert tuple(review_db.execute(text('SELECT location_printer_id,remaining_weight_g FROM spools WHERE id=199')).one())==(None,500)


def test_daemon_foreign_previous_location_is_not_transferred(daemon_ams, review_db):
    send,_,_=daemon_ams
    review_db.execute(text("INSERT INTO printers(id,name,org_id) VALUES(200,'Synthetic foreign prior',2)"))
    review_db.execute(text('UPDATE spools SET location_printer_id=200 WHERE id=199'));review_db.commit()
    send(50)
    assert tuple(review_db.execute(text('SELECT location_printer_id,remaining_weight_g FROM spools WHERE id=199')).one())==(200,500)
    assert review_db.execute(text('SELECT color_hex FROM filament_slots WHERE id=199')).scalar_one() is None



def test_daemon_same_org_spool_move_keeps_both_sides_of_association_consistent(daemon_ams, review_db):
    send,_,_=daemon_ams
    review_db.execute(text("INSERT INTO printers(id,name,org_id) VALUES(201,'Synthetic own previous',1)"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,org_id,rfid_tag,initial_weight_g,remaining_weight_g,location_printer_id,location_slot) VALUES(200,199,1,'synthetic-incoming-tag',1000,400,201,1)"))
    review_db.execute(text('UPDATE spools SET location_printer_id=199,location_slot=1 WHERE id=199'))
    review_db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE id=199'))
    review_db.execute(text("INSERT INTO filament_slots(id,printer_id,slot_number,filament_type,assigned_spool_id,spool_confirmed) VALUES(201,201,1,'PLA',200,1)"));review_db.commit()
    send(trays=[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-incoming-tag','remain':50}])
    assert review_db.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE id=199')).scalar_one()==200
    assert tuple(review_db.execute(text('SELECT location_printer_id,location_slot FROM spools WHERE id=199')).one())==(None,None)
    assert review_db.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE id=201')).scalar_one() is None
    assert tuple(review_db.execute(text('SELECT location_printer_id,location_slot,remaining_weight_g FROM spools WHERE id=200')).one())==(199,1,500)


def test_daemon_duplicate_rfid_destinations_refuse_entire_batch(daemon_ams, review_db):
    send,monitor,_=daemon_ams
    tray={'tray_type':'PLA','tag_uid':'synthetic-own-tag','remain':0}
    send(trays=[dict(tray,id='0'),dict(tray,id='1')])
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
    assert review_db.execute(text('SELECT color_hex FROM filament_slots WHERE id=199')).scalar_one() is None
    assert getattr(monitor,'_last_ams_slot_sync',0)==0


def test_daemon_foreign_prior_assignment_without_location_is_refused(daemon_ams, review_db):
    send,_,_=daemon_ams
    review_db.execute(text("INSERT INTO printers(id,name,org_id) VALUES(201,'Synthetic foreign',2)"))
    review_db.execute(text("INSERT INTO filament_slots(id,printer_id,slot_number,filament_type,assigned_spool_id) VALUES(201,201,1,'PLA',199)"));review_db.commit()
    send(0)
    assert review_db.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE id=201')).scalar_one()==199
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
    assert review_db.execute(text('SELECT color_hex FROM filament_slots WHERE id=199')).scalar_one() is None


def test_daemon_same_printer_spool_move_preserves_prior_material_metadata(daemon_ams, review_db):
    send,_,_=daemon_ams
    review_db.execute(text('UPDATE spools SET location_printer_id=199,location_slot=2 WHERE id=199'))
    review_db.execute(text("INSERT INTO filament_slots(id,printer_id,slot_number,filament_type,color_hex,assigned_spool_id,spool_confirmed) VALUES(201,199,2,'PLA','ABCDEF',199,1)"));review_db.commit()
    send(0)
    assert tuple(review_db.execute(text('SELECT assigned_spool_id,filament_type,color_hex FROM filament_slots WHERE id=201')).one())==(None,'PLA','ABCDEF')
    assert tuple(review_db.execute(text('SELECT location_printer_id,location_slot,remaining_weight_g FROM spools WHERE id=199')).one())==(199,1,0)


def test_daemon_move_commit_failure_rolls_back_entire_bidirectional_graph(daemon_ams, review_db, monkeypatch):
    from modules.printers.monitors import mqtt_printer as module
    send,monitor,_=daemon_ams
    review_db.execute(text("INSERT INTO printers(id,name,org_id) VALUES(201,'Synthetic prior own',1)"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,org_id,rfid_tag,initial_weight_g,remaining_weight_g,location_printer_id,location_slot) VALUES(200,199,1,'synthetic-incoming-tag',1000,400,201,1)"))
    review_db.execute(text('UPDATE spools SET location_printer_id=199,location_slot=1 WHERE id=199'))
    review_db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE id=199'))
    review_db.execute(text("INSERT INTO filament_slots(id,printer_id,slot_number,filament_type,assigned_spool_id,spool_confirmed) VALUES(201,201,1,'PLA',200,1)"));review_db.commit()
    def graph():
        return (review_db.execute(text('SELECT id,location_printer_id,location_slot,remaining_weight_g FROM spools WHERE id IN (199,200) ORDER BY id')).all(),
                review_db.execute(text('SELECT id,assigned_spool_id,spool_confirmed,color_hex FROM filament_slots ORDER BY id')).all())
    before=graph();original=module.get_db;failed=[False]
    class FaultConnection:
        def __init__(self,conn):self.conn=conn;self.inventory=False
        def __getattr__(self,name):return getattr(self.conn,name)
        def execute(self,statement,parameters=()):
            if statement.startswith('UPDATE filament_slots'):self.inventory=True
            return self.conn.execute(statement,parameters)
        def commit(self):
            if self.inventory and not failed[0]:
                failed[0]=True;raise RuntimeError('synthetic-commit-refusal')
            self.conn.commit();self.inventory=False
        def rollback(self):self.conn.rollback();self.inventory=False
    @contextmanager
    def database():
        with original() as conn:yield FaultConnection(conn)
    monkeypatch.setattr(module,'get_db',database)
    send(trays=[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-incoming-tag','remain':50}])
    assert failed[0] and graph()==before
    assert getattr(monitor,'_last_ams_slot_sync',0)==0
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:1011.0))
    send(trays=[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-incoming-tag','remain':50}])
    assert tuple(review_db.execute(text('SELECT location_printer_id,location_slot FROM spools WHERE id=199')).one())==(None,None)
    assert review_db.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE id=201')).scalar_one() is None
    assert monitor._last_ams_slot_sync==1011


def test_daemon_displaced_spool_contradictory_location_refuses_batch(daemon_ams, review_db):
    send,monitor,_=daemon_ams
    review_db.execute(text("INSERT INTO spools(id,filament_id,org_id,rfid_tag,initial_weight_g,remaining_weight_g) VALUES(200,199,1,'synthetic-incoming-tag',1000,400)"))
    review_db.execute(text('UPDATE spools SET location_printer_id=199,location_slot=2 WHERE id=199'))
    review_db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE id=199'));review_db.commit()
    send(trays=[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-incoming-tag','remain':50}])
    assert review_db.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE id=199')).scalar_one()==199
    assert tuple(review_db.execute(text('SELECT location_printer_id,remaining_weight_g FROM spools WHERE id=200')).one())==(None,400)
    assert getattr(monitor,'_last_ams_slot_sync',0)==0


@pytest.mark.parametrize("fail_commit", [False, True])
def test_daemon_two_spool_swap_preserves_complete_final_graph(daemon_ams, review_db, monkeypatch, fail_commit):
    send,_,_=daemon_ams
    review_db.execute(text('UPDATE spools SET location_printer_id=199,location_slot=1 WHERE id=199'))
    review_db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE id=199'))
    review_db.execute(text("INSERT INTO spools(id,filament_id,org_id,rfid_tag,initial_weight_g,remaining_weight_g,location_printer_id,location_slot) VALUES(200,199,1,'synthetic-second-tag',1000,400,199,2)"))
    review_db.execute(text("INSERT INTO filament_slots(id,printer_id,slot_number,filament_type,assigned_spool_id,spool_confirmed) VALUES(201,199,2,'PLA',200,1)"));review_db.commit()
    trays=[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-second-tag','remain':50}, {'id':'1','tray_type':'PLA','tag_uid':'synthetic-own-tag','remain':25}]
    if fail_commit:
        from modules.printers.monitors import mqtt_printer as module
        original=module.get_db;failed=[False]
        class FaultConnection:
            def __init__(self,conn):self.conn=conn;self.inventory=False
            def __getattr__(self,name):return getattr(self.conn,name)
            def execute(self,statement,parameters=()):
                if statement.startswith('UPDATE filament_slots'):self.inventory=True
                return self.conn.execute(statement,parameters)
            def commit(self):
                if self.inventory and not failed[0]:
                    failed[0]=True;raise RuntimeError('Synthetic swap commit refusal')
                self.conn.commit();self.inventory=False
            def rollback(self):self.conn.rollback();self.inventory=False
        @contextmanager
        def database():
            with original() as conn:yield FaultConnection(conn)
        monkeypatch.setattr(module,'get_db',database)
        send(trays=trays)
        assert failed[0]
        assert review_db.execute(text('SELECT slot_number,assigned_spool_id FROM filament_slots WHERE printer_id=199 ORDER BY slot_number')).all()==[(1,199),(2,200)]
        assert review_db.execute(text('SELECT id,location_printer_id,location_slot,remaining_weight_g FROM spools WHERE id IN (199,200) ORDER BY id')).all()==[(199,199,1,500),(200,199,2,400)]
        monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:1011.0))
    send(trays=trays)
    assert review_db.execute(text('SELECT slot_number,assigned_spool_id FROM filament_slots WHERE printer_id=199 ORDER BY slot_number')).all()==[(1,200),(2,199)]
    assert review_db.execute(text('SELECT id,location_printer_id,location_slot,remaining_weight_g FROM spools WHERE id IN (199,200) ORDER BY id')).all()==[(199,199,2,250),(200,199,1,500)]


def test_daemon_sparse_unit_and_tray_use_physical_identity(daemon_ams, review_db):
    send,monitor,_=daemon_ams
    send(units=[{'id':'2','tray':[{'id':'2','tray_type':'PLA','tag_uid':'synthetic-own-tag','remain':25}]}])
    assert review_db.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE printer_id=199 AND slot_number=11')).scalar_one()==199
    assert review_db.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE id=199')).scalar_one() is None
    assert monitor._state['_ams_remain']=={11:25}


@pytest.mark.parametrize('bad_id', [None, True, 1.5, 'bad', -1, 4, float('inf')])
def test_daemon_mixed_invalid_identity_refuses_inventory_without_losing_valid_cache(daemon_ams, review_db, bad_id):
    send,monitor,_=daemon_ams
    send(units=[{'id':'0','tray':[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-own-tag','remain':25}, {'id':bad_id,'tray_type':'PETG','remain':50}]}])
    assert review_db.execute(text('SELECT assigned_spool_id,color_hex FROM filament_slots WHERE id=199')).one()==(None,None)
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
    assert getattr(monitor,'_last_ams_slot_sync',0)==0
    assert monitor._state['_ams_remain']=={1:25}


def test_daemon_duplicate_physical_slot_refuses_inventory_and_omits_ambiguous_cache(daemon_ams, review_db):
    send,monitor,_=daemon_ams
    send(trays=[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-own-tag','remain':25}, {'id':'0','tray_type':'PETG','tag_uid':'synthetic-other-tag','remain':50}])
    assert review_db.execute(text('SELECT assigned_spool_id,color_hex FROM filament_slots WHERE id=199')).one()==(None,None)
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one()==500
    assert monitor._state['_ams_remain']=={}


@pytest.mark.parametrize("packet", [{"gcode_state":"IDLE"}, {"ams":None}, {"ams":{}}, {"ams":{"ams":"invalid"}}])
def test_heartbeat_cannot_refresh_cached_ams_inventory(daemon_ams, review_db, packet):
    send, monitor, _ = daemon_ams
    send(50)
    review_db.execute(text("UPDATE filament_slots SET loaded_at='2000-01-01 00:00:00' WHERE printer_id=199"))
    review_db.commit()
    monitor._last_ams_slot_sync = 0
    monitor._last_heartbeat = 0
    monitor._on_status(SimpleNamespace(raw_data={'print': packet}))
    review_db.expire_all()
    assert str(review_db.execute(text('SELECT loaded_at FROM filament_slots WHERE id=199')).scalar_one()) == '2000-01-01 00:00:00'
    assert monitor._last_ams_slot_sync == 0

@pytest.mark.parametrize('material', [None, 42, False, [], {}, '   ', 'PLA???', 'unrecognized'])
def test_partial_material_preserves_known_inventory_and_freshness(daemon_ams, review_db, monkeypatch, material):
    from modules.printers.monitors import mqtt_printer as module
    send, _, _ = daemon_ams
    send(25)
    review_db.execute(text("UPDATE filament_slots SET filament_type='PETG',loaded_at='2000-01-01 00:00:00' WHERE id=199"))
    review_db.commit()
    before = tuple(review_db.execute(text('SELECT filament_type,color_hex,assigned_spool_id,loaded_at FROM filament_slots WHERE id=199')).one())
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda:1061.0))
    tray = {'id':'0', 'tray_color':'FFFFFF', 'remain':0}
    if material is not None:
        tray['tray_type'] = material
    send(trays=[tray])
    assert tuple(review_db.execute(text('SELECT filament_type,color_hex,assigned_spool_id,loaded_at FROM filament_slots WHERE id=199')).one()) == before
    assert review_db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=199')).scalar_one() == 250


def test_explicit_empty_material_still_clears_slot(daemon_ams, review_db, monkeypatch):
    from modules.printers.monitors import mqtt_printer as module
    send, _, _ = daemon_ams
    send(25)
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda:1061.0))
    send(trays=[{'id':'0', 'tray_type':''}])
    assert tuple(review_db.execute(text('SELECT filament_type,color_hex FROM filament_slots WHERE id=199')).one()) == ('empty', None)


def test_partial_material_does_not_prune_absent_ht_slots(daemon_ams, review_db, monkeypatch):
    from modules.printers.monitors import mqtt_printer as module
    send, _, _ = daemon_ams
    send(25)
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) VALUES(199,17,'PETG','123ABC')")); review_db.commit()
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:1061.0))
    send(trays=[{'id':'0','tray_type':None}])
    assert tuple(review_db.execute(text('SELECT filament_type,color_hex FROM filament_slots WHERE printer_id=199 AND slot_number=17')).one()) == ('PETG','123ABC')
