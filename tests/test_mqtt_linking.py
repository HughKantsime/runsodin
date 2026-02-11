"""
O.D.I.N. MQTT Auto-Linking — Unit Tests

Tests the _job_started() name-matching and layer-count-fallback logic
from mqtt_monitor.PrinterMonitor.

Uses an in-memory SQLite database — no running server needed.
"""

import sys
import os
import sqlite3
import time
import pytest
from unittest.mock import MagicMock, patch

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def _create_tables(conn):
    """Create the minimal tables needed for _job_started()."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_id INTEGER,
            item_name TEXT,
            printer_id INTEGER,
            status TEXT DEFAULT 'PENDING',
            scheduled_start TEXT
        );

        CREATE TABLE IF NOT EXISTS models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT
        );

        CREATE TABLE IF NOT EXISTS print_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_id INTEGER,
            filename TEXT,
            layer_count INTEGER
        );

        CREATE TABLE IF NOT EXISTS print_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_id INTEGER,
            job_id TEXT,
            filename TEXT,
            job_name TEXT,
            started_at TEXT,
            status TEXT,
            total_layers INTEGER,
            bed_temp_target REAL,
            nozzle_temp_target REAL,
            scheduled_job_id INTEGER
        );
    """)
    conn.commit()


def _insert_job(conn, job_id, model_id, item_name, printer_id, status="PENDING"):
    conn.execute(
        "INSERT INTO jobs (id, model_id, item_name, printer_id, status) VALUES (?, ?, ?, ?, ?)",
        (job_id, model_id, item_name, printer_id, status),
    )
    conn.commit()


def _insert_model(conn, model_id, name):
    conn.execute("INSERT INTO models (id, name) VALUES (?, ?)", (model_id, name))
    conn.commit()


def _insert_print_file(conn, model_id, filename, layer_count):
    conn.execute(
        "INSERT INTO print_files (model_id, filename, layer_count) VALUES (?, ?, ?)",
        (model_id, filename, layer_count),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    """In-memory SQLite database with required tables."""
    conn = sqlite3.connect(":memory:")
    _create_tables(conn)
    yield conn
    conn.close()


@pytest.fixture()
def db_path(tmp_path, db):
    """Write the in-memory DB to a temp file (needed for monitor which opens by path)."""
    path = str(tmp_path / "test_odin.db")
    file_conn = sqlite3.connect(path)
    _create_tables(file_conn)
    file_conn.close()
    return path


# ---------------------------------------------------------------------------
# Helper: run _job_started with controlled state
# ---------------------------------------------------------------------------

def _run_job_started(db_path, printer_id, mqtt_state):
    """
    Instantiate a PrinterMonitor with mocked dependencies, set state,
    and call _job_started(). Returns the monitor instance for inspection.
    """
    # Import with mocked dependencies
    with patch.dict("sys.modules", {
        "crypto": MagicMock(),
        "bambu_adapter": MagicMock(),
        "printer_events": MagicMock(),
        "ws_hub": MagicMock(),
        "moonraker_monitor": MagicMock(),
    }):
        # Patch DB_PATH before importing
        with patch.dict(os.environ, {"DATABASE_PATH": db_path}):
            # Force reimport to pick up patched DB_PATH
            if "mqtt_monitor" in sys.modules:
                del sys.modules["mqtt_monitor"]
            import mqtt_monitor
            mqtt_monitor.DB_PATH = db_path

            monitor = mqtt_monitor.PrinterMonitor(
                printer_id=printer_id,
                name="TestPrinter",
                ip="192.168.1.100",
                serial="TESTSERIAL",
                access_code="12345678",
            )
            monitor._state = mqtt_state
            monitor._job_started()
            return monitor


def _setup_db(db_path, jobs=None, models=None, print_files=None):
    """Populate the temp database with test data."""
    conn = sqlite3.connect(db_path)
    _create_tables(conn)
    for m in (models or []):
        _insert_model(conn, m["id"], m["name"])
    for pf in (print_files or []):
        _insert_print_file(conn, pf["model_id"], pf["filename"], pf["layer_count"])
    for j in (jobs or []):
        _insert_job(
            conn, j["id"], j.get("model_id"), j.get("item_name"),
            j.get("printer_id"), j.get("status", "PENDING"),
        )
    conn.close()


# ---------------------------------------------------------------------------
# Tests: Name Matching (Strategy 1)
# ---------------------------------------------------------------------------

class TestNameMatching:
    """Strategy 1: substring match on filename, item_name, or model_name."""

    def test_exact_model_name_match(self, db_path):
        _setup_db(db_path,
            models=[{"id": 1, "name": "baby_yoda"}],
            print_files=[{"model_id": 1, "filename": "baby_yoda.3mf", "layer_count": 100}],
            jobs=[{"id": 10, "model_id": 1, "item_name": "Baby Yoda Build", "printer_id": 1}],
        )
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "baby_yoda.3mf",
            "gcode_file": "baby_yoda.gcode.3mf",
            "total_layer_num": 100,
        })
        assert monitor._linked_job_id == 10

    def test_substring_target_in_job_base(self, db_path):
        """MQTT sends 'baby_yoda_v2', DB has model named 'baby_yoda_v2_final'."""
        _setup_db(db_path,
            models=[{"id": 1, "name": "baby_yoda_v2_final"}],
            print_files=[{"model_id": 1, "filename": "baby_yoda_v2_final.3mf", "layer_count": 50}],
            jobs=[{"id": 10, "model_id": 1, "item_name": "Yoda", "printer_id": 1}],
        )
        # "baby_yoda_v2_final" contains... let's test with the reverse:
        # MQTT sends longer name containing the DB model name
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "baby_yoda_v2_final_plate1.3mf",
            "gcode_file": "",
            "total_layer_num": 50,
        })
        assert monitor._linked_job_id == 10

    def test_substring_job_base_in_target(self, db_path):
        """MQTT sends 'widget', DB has filename 'widget_left_arm.3mf'."""
        _setup_db(db_path,
            models=[{"id": 1, "name": "widget_left_arm"}],
            print_files=[{"model_id": 1, "filename": "widget_left_arm.3mf", "layer_count": 80}],
            jobs=[{"id": 10, "model_id": 1, "item_name": "Widget Arms", "printer_id": 1}],
        )
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "widget.3mf",
            "gcode_file": "",
            "total_layer_num": 80,
        })
        # "widget" is in "widget_left_arm" → match
        assert monitor._linked_job_id == 10

    def test_case_insensitive(self, db_path):
        """MQTT sends 'MyPart.3mf', DB model is 'mypart'."""
        _setup_db(db_path,
            models=[{"id": 1, "name": "mypart"}],
            print_files=[{"model_id": 1, "filename": "mypart.3mf", "layer_count": 60}],
            jobs=[{"id": 10, "model_id": 1, "item_name": "mypart job", "printer_id": 1}],
        )
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "MyPart.3mf",
            "gcode_file": "",
            "total_layer_num": 60,
        })
        assert monitor._linked_job_id == 10

    def test_extension_stripping(self, db_path):
        """Extensions like .3mf and .gcode are stripped before matching."""
        _setup_db(db_path,
            models=[{"id": 1, "name": "bracket"}],
            print_files=[{"model_id": 1, "filename": "bracket.gcode", "layer_count": 40}],
            jobs=[{"id": 10, "model_id": 1, "item_name": "Bracket Print", "printer_id": 1}],
        )
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "bracket.3mf",
            "gcode_file": "",
            "total_layer_num": 40,
        })
        assert monitor._linked_job_id == 10

    def test_only_matches_same_printer(self, db_path):
        """Jobs assigned to a different printer should not match."""
        _setup_db(db_path,
            models=[{"id": 1, "name": "part_a"}],
            print_files=[{"model_id": 1, "filename": "part_a.3mf", "layer_count": 30}],
            jobs=[{"id": 10, "model_id": 1, "item_name": "Part A", "printer_id": 2}],  # different printer
        )
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "part_a.3mf",
            "gcode_file": "",
            "total_layer_num": 30,
        })
        assert monitor._linked_job_id is None

    def test_only_matches_scheduled_or_pending(self, db_path):
        """Completed/cancelled jobs should not be candidates."""
        _setup_db(db_path,
            models=[{"id": 1, "name": "done_part"}],
            print_files=[{"model_id": 1, "filename": "done_part.3mf", "layer_count": 20}],
            jobs=[{"id": 10, "model_id": 1, "item_name": "Done Part", "printer_id": 1, "status": "COMPLETED"}],
        )
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "done_part.3mf",
            "gcode_file": "",
            "total_layer_num": 20,
        })
        assert monitor._linked_job_id is None


# ---------------------------------------------------------------------------
# Tests: Layer Count Matching (Strategy 2)
# ---------------------------------------------------------------------------

class TestLayerCountMatching:
    """Strategy 2: unique layer count match as fallback."""

    def test_unique_layer_match(self, db_path):
        """No name match, but exactly one job has matching layer count."""
        _setup_db(db_path,
            models=[{"id": 1, "name": "alpha"}],
            print_files=[{"model_id": 1, "filename": "alpha.3mf", "layer_count": 157}],
            jobs=[{"id": 10, "model_id": 1, "item_name": "Alpha Build", "printer_id": 1}],
        )
        # MQTT sends a completely different name but same layer count
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "completely_different_name.3mf",
            "gcode_file": "",
            "total_layer_num": 157,
        })
        assert monitor._linked_job_id == 10

    def test_multiple_layer_matches_no_link(self, db_path):
        """Two jobs match the same layer count → ambiguous, no link."""
        _setup_db(db_path,
            models=[
                {"id": 1, "name": "part_x"},
                {"id": 2, "name": "part_y"},
            ],
            print_files=[
                {"model_id": 1, "filename": "part_x.3mf", "layer_count": 200},
                {"model_id": 2, "filename": "part_y.3mf", "layer_count": 200},
            ],
            jobs=[
                {"id": 10, "model_id": 1, "item_name": "Part X", "printer_id": 1},
                {"id": 11, "model_id": 2, "item_name": "Part Y", "printer_id": 1},
            ],
        )
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "unknown_part.3mf",
            "gcode_file": "",
            "total_layer_num": 200,
        })
        assert monitor._linked_job_id is None

    def test_zero_layers_skips_strategy2(self, db_path):
        """total_layers=0 means Strategy 2 is skipped entirely."""
        _setup_db(db_path,
            models=[{"id": 1, "name": "zeropart"}],
            print_files=[{"model_id": 1, "filename": "zeropart.3mf", "layer_count": 0}],
            jobs=[{"id": 10, "model_id": 1, "item_name": "Zero Part", "printer_id": 1}],
        )
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "nomatch.3mf",
            "gcode_file": "",
            "total_layer_num": 0,
        })
        assert monitor._linked_job_id is None


# ---------------------------------------------------------------------------
# Tests: No candidates
# ---------------------------------------------------------------------------

class TestNoCandidates:
    def test_no_scheduled_jobs(self, db_path):
        """No jobs in the DB at all → print_jobs record created, no link."""
        _setup_db(db_path)
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "orphan.3mf",
            "gcode_file": "",
            "total_layer_num": 50,
        })
        assert monitor._linked_job_id is None
        # Verify print_jobs record was still created
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM print_jobs").fetchall()
        conn.close()
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Tests: Status update on link
# ---------------------------------------------------------------------------

class TestStatusUpdate:
    def test_linked_job_set_to_printing(self, db_path):
        """When a job is auto-linked, its status should update to PRINTING."""
        _setup_db(db_path,
            models=[{"id": 1, "name": "status_test"}],
            print_files=[{"model_id": 1, "filename": "status_test.3mf", "layer_count": 75}],
            jobs=[{"id": 10, "model_id": 1, "item_name": "Status Test", "printer_id": 1}],
        )
        monitor = _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "status_test.3mf",
            "gcode_file": "",
            "total_layer_num": 75,
        })
        assert monitor._linked_job_id == 10

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT status FROM jobs WHERE id = 10").fetchone()
        conn.close()
        assert row[0] == "PRINTING"

    def test_unlinked_job_status_unchanged(self, db_path):
        """Jobs that aren't linked should keep their original status."""
        _setup_db(db_path,
            models=[{"id": 1, "name": "nolink"}],
            print_files=[{"model_id": 1, "filename": "nolink.3mf", "layer_count": 50}],
            jobs=[{"id": 10, "model_id": 1, "item_name": "NoLink", "printer_id": 1}],
        )
        _run_job_started(db_path, printer_id=1, mqtt_state={
            "subtask_name": "totally_different.3mf",
            "gcode_file": "",
            "total_layer_num": 999,  # No match
        })
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT status FROM jobs WHERE id = 10").fetchone()
        conn.close()
        assert row[0] == "PENDING"
