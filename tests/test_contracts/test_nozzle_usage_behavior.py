"""Execute nozzle usage SQL against isolated storage, without printer commands."""
from contextlib import contextmanager
import sqlite3
import pytest
from modules.notifications import printer_health

@pytest.fixture
def usage_store(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic-nozzle-usage.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE nozzle_lifecycle(id INTEGER PRIMARY KEY,printer_id INTEGER,removed_at TEXT,print_hours_accumulated REAL,print_count INTEGER)')
        connection.executemany('INSERT INTO nozzle_lifecycle VALUES(?,?,?,?,?)', [(1,1,None,2.5,2),(2,1,'2026-10-01',7,4),(3,2,None,4,3)])
    @contextmanager
    def database():
        connection = sqlite3.connect(path)
        try:
            yield connection
        finally:
            connection.close()
    monkeypatch.setattr(printer_health, 'get_db', database)
    return path

def test_only_active_target_nozzle_accumulates_successful_usage(usage_store):
    printer_health.increment_nozzle_lifecycle(1, 1.25, 1)
    with sqlite3.connect(usage_store) as connection:
        rows = connection.execute('SELECT id,print_hours_accumulated,print_count FROM nozzle_lifecycle ORDER BY id').fetchall()
    assert rows == [(1,3.75,3),(2,7,4),(3,4,3)]

def test_repeated_increment_preserves_fractional_hours(usage_store):
    printer_health.increment_nozzle_lifecycle(1, 0.125, 1)
    printer_health.increment_nozzle_lifecycle(1, 0.125, 1)
    with sqlite3.connect(usage_store) as connection:
        assert connection.execute('SELECT print_hours_accumulated,print_count FROM nozzle_lifecycle WHERE id=1').fetchone() == (2.75,4)

def test_no_active_nozzle_does_not_rewrite_retired_history(usage_store):
    with sqlite3.connect(usage_store) as connection:
        connection.execute("UPDATE nozzle_lifecycle SET removed_at='2026-10-02' WHERE id=1")
    printer_health.increment_nozzle_lifecycle(1, 1.5, 1)
    with sqlite3.connect(usage_store) as connection:
        assert connection.execute('SELECT print_hours_accumulated,print_count FROM nozzle_lifecycle WHERE id=1').fetchone() == (2.5,2)
