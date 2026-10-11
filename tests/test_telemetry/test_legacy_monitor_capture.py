"""Execute the real callback against owned SQLite; substitute outbound boundaries."""
from contextlib import contextmanager
from types import SimpleNamespace
import sqlite3
import pytest

@pytest.fixture
def capture_store(tmp_path, monkeypatch):
    from modules.printers.monitors import mqtt_printer as module
    path = tmp_path / 'synthetic-monitor.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('''
        CREATE TABLE printers(id INTEGER PRIMARY KEY, lights_toggled_at TEXT, last_seen TEXT,
          bed_temp REAL,bed_target_temp REAL,nozzle_temp REAL,nozzle_target_temp REAL,
          gcode_state TEXT,print_stage TEXT,hms_errors TEXT,lights_on INTEGER,
          nozzle_type TEXT,nozzle_diameter REAL,fan_speed REAL,machine_type TEXT);
        INSERT INTO printers(id) VALUES(1);
        CREATE TABLE ams_telemetry(id INTEGER PRIMARY KEY,printer_id INTEGER,ams_unit INTEGER,
          humidity INTEGER,temperature REAL,recorded_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE printer_telemetry(id INTEGER PRIMARY KEY,printer_id INTEGER,
          bed_temp REAL,nozzle_temp REAL,bed_target REAL,nozzle_target REAL,fan_speed REAL,
          recorded_at TEXT DEFAULT(datetime('now')));
        CREATE TABLE hms_error_history(id INTEGER PRIMARY KEY,printer_id INTEGER,
          code TEXT,message TEXT,severity TEXT,source TEXT,occurred_at TEXT DEFAULT(datetime('now')));
        ''')
    @contextmanager
    def database():
        connection = sqlite3.connect(path)
        try:
            yield connection
        finally:
            connection.close()
    clock = [1000.0]
    monkeypatch.setattr(module, 'get_db', database)
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(module, 'mqtt_republish', None)
    monkeypatch.setattr(module, 'ws_push', lambda *args, **kwargs: None)
    monitor = module.PrinterMonitor(1, 'Synthetic monitor', '192.0.2.1', 'synthetic', '')
    monitor._camera_discovered = True
    monkeypatch.setattr(monitor, '_on_state_change', lambda *args: None)
    monkeypatch.setattr(monitor, '_check_spool_levels', lambda: None)
    monkeypatch.setattr(monitor, '_update_progress', lambda: None)
    send_hms = object()
    def send(state='IDLE', hms=send_hms, filename=None):
        monitor._current_job_id = 99 if state == 'RUNNING' else None
        packet = {
            'gcode_state':state, 'bed_temper':60, 'nozzle_temper':210,
            'ams':{'ams':[{'humidity':'3','temp':'22.5'}, {'humidity':'4','temp':'26'}]},
        }
        if filename is not None:
            packet['subtask_name'] = filename
        if hms is not send_hms:
            packet['hms'] = hms
        monitor._on_status(SimpleNamespace(raw_data={'print':packet}))
    send.module = module
    return path, clock, send


def rows(path, table):
    with sqlite3.connect(path) as db:
        return db.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()


def test_ams_capture_five_minute_boundary_and_multiple_units(capture_store):
    path, clock, send = capture_store
    send()
    assert [(r[2],r[3],r[4]) for r in rows(path,'ams_telemetry')] == [(0,3,22.5),(1,4,26.0)]
    clock[0] = 1290
    send()
    assert len(rows(path,'ams_telemetry')) == 2
    clock[0] = 1300
    send()
    assert len(rows(path,'ams_telemetry')) == 4


def test_active_temperature_snapshots_sixty_second_boundary(capture_store):
    path, clock, send = capture_store
    send('RUNNING')
    assert len(rows(path,'printer_telemetry')) == 1
    clock[0] = 1050
    send('RUNNING')
    assert len(rows(path,'printer_telemetry')) == 1
    clock[0] = 1060
    send('RUNNING')
    result = rows(path,'printer_telemetry')
    assert len(result) == 2
    assert [(r[2],r[3]) for r in result] == [(60.0,210.0),(60.0,210.0)]


def test_idle_does_not_capture_print_temperature_snapshots(capture_store):
    path, clock, send = capture_store
    send()
    clock[0] = 1060
    send()
    assert rows(path,'printer_telemetry') == []
    assert len(rows(path,'ams_telemetry')) == 2


def test_actual_callback_retains_eighty_nine_days_and_deletes_ninety_one(capture_store):
    path, clock, send = capture_store
    with sqlite3.connect(path) as db:
        for age in (8,89,91):
            db.execute("INSERT INTO ams_telemetry(printer_id,ams_unit,humidity,temperature,recorded_at) VALUES(2,0,2,?,datetime('now',?))", (age,f'-{age} days'))
            db.execute("INSERT INTO printer_telemetry(printer_id,bed_temp,recorded_at) VALUES(2,?,datetime('now',?))", (age,f'-{age} days'))
    send('RUNNING')
    assert [r[4] for r in rows(path,'ams_telemetry') if r[1]==2] == [8.0,89.0]
    assert [r[2] for r in rows(path,'printer_telemetry') if r[1]==2] == [8.0,89.0]


@pytest.mark.parametrize('table,state', [('ams_telemetry','IDLE'),('printer_telemetry','RUNNING')])
def test_failed_capture_retries_on_next_heartbeat(capture_store, table, state):
    path, clock, send = capture_store
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TRIGGER reject_capture BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END")
    send(state)
    assert rows(path,table) == []
    with sqlite3.connect(path) as db:
        db.execute('DROP TRIGGER reject_capture')
    clock[0] = 1010
    send(state)
    assert len(rows(path,table)) == (2 if table == 'ams_telemetry' else 1)


def test_failed_second_ams_unit_rolls_back_entire_capture(capture_store):
    path, clock, send = capture_store
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON ams_telemetry WHEN NEW.ams_unit=1 BEGIN SELECT RAISE(ABORT,'synthetic second-unit failure'); END")
    send()
    assert rows(path,'ams_telemetry') == []
    with sqlite3.connect(path) as db:
        db.execute('DROP TRIGGER reject_second')
    clock[0] = 1010
    send()
    assert [(r[2],r[3],r[4]) for r in rows(path,'ams_telemetry')] == [(0,3,22.5),(1,4,26.0)]


@pytest.mark.parametrize('table,state', [('ams_telemetry','IDLE'),('printer_telemetry','RUNNING')])
def test_retention_failure_rolls_back_new_samples_and_retries(capture_store, table, state):
    path, clock, send = capture_store
    with sqlite3.connect(path) as db:
        db.execute(f"INSERT INTO {table}(printer_id,recorded_at) VALUES(2,datetime('now','-91 days'))")
        db.execute(f"CREATE TRIGGER reject_cleanup BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'synthetic retention failure'); END")
    original = rows(path,table)
    send(state)
    assert rows(path,table) == original
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT gcode_state FROM printers WHERE id=1').fetchone()[0] == state
        db.execute('DROP TRIGGER reject_cleanup')
    clock[0] = 1010
    send(state)
    result = rows(path,table)
    assert len(result) == (2 if table == 'ams_telemetry' else 1)
    assert all(row[1] == 1 for row in result)


@pytest.mark.parametrize('table,state', [('ams_telemetry','IDLE'),('printer_telemetry','RUNNING')])
def test_known_precommit_failure_rolls_back_and_retries(capture_store, monkeypatch, table, state):
    path, clock, send = capture_store
    original_database = send.module.get_db
    fail_once = [True]
    class Connection:
        def __init__(self, actual):
            self.actual = actual
            self.capture_pending = False
        def execute(self, statement, *args):
            if statement.startswith('INSERT INTO '+table+' '):
                self.capture_pending = True
            return self.actual.execute(statement, *args)
        def commit(self):
            if self.capture_pending and fail_once[0]:
                fail_once[0] = False
                raise RuntimeError('Synthetic failure before database commit')
            return self.actual.commit()
        def rollback(self):
            self.capture_pending = False
            return self.actual.rollback()
        def __getattr__(self, name):
            return getattr(self.actual, name)
    @contextmanager
    def database():
        with original_database() as actual:
            yield Connection(actual)
    monkeypatch.setattr(send.module, 'get_db', database)
    send(state)
    assert not fail_once[0]
    assert rows(path,table) == []
    clock[0] = 1010
    send(state)
    assert len(rows(path,table)) == (2 if table=='ams_telemetry' else 1)
