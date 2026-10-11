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
    def send(state='IDLE'):
        monitor._current_job_id = 99 if state == 'RUNNING' else None
        monitor._on_status(SimpleNamespace(raw_data={'print': {
            'gcode_state':state, 'bed_temper':60, 'nozzle_temper':210,
            'ams':{'ams':[{'humidity':'3','temp':'22.5'}, {'humidity':'4','temp':'26'}]},
        }}))
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
