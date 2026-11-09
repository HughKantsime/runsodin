"""Fixtures for the ODIN integration tests."""
import copy

import pytest

URL = "http://odin.example.test:8000"
STATUS_URL = URL + "/api/v1/integrations/home-assistant/status"

STATUS = {
    "schema_version": 1,
    "odin_version": "2.0.51",
    "generated_at": "2026-11-09T01:00:00Z",
    "fleet": {"printing": 1, "paused": 1, "error": 0, "offline": 0, "idle": 0, "total": 2, "queue_length": 4},
    "printers": [
        {"id": 1, "name": "X1C", "model": "Bambu Lab X1C", "api_type": "bambu", "status": "printing",
         "online": True, "raw_state": "RUNNING", "progress": 42.5, "current_layer": 12, "total_layers": 118,
         "remaining_minutes": 90.0, "eta": "2026-11-09T02:30:00Z", "job_name": "Bracket",
         "active_errors": 0, "last_error": None, "last_seen": "2026-11-09T00:59:58Z", "camera_available": True},
        {"id": 4, "name": "Kobra S1", "model": "Anycubic Kobra S1", "api_type": "anycubic_lan", "status": "paused",
         "online": True, "raw_state": "PAUSED", "progress": None, "current_layer": None, "total_layers": None,
         "remaining_minutes": None, "eta": None, "job_name": None, "active_errors": 1,
         "last_error": {"code": "PRINT_FAILED", "message": "Nozzle clog", "at": "2026-11-08T23:36:50Z"},
         "last_seen": "2026-11-09T00:59:58Z", "camera_available": False},
    ],
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def status():
    return copy.deepcopy(STATUS)
