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


# Status schema 2 (print-floor fields), built on the schema 1 payload above.
STATUS_V2 = copy.deepcopy(STATUS)
STATUS_V2["schema_version"] = 2
STATUS_V2["fleet"].update({
    "today": {"plates_done": 7, "plates_failed": 1, "since": "2026-11-09T00:00:00Z"},
    "queued_hours": 12.46,
    "queued_unestimated": 2,
    "next_unassigned": [
        {"id": 31, "name": "Gear", "held": False, "est_minutes": 45, "copies_on_plate": 2, "filaments": [],
         "plate": None, "ams_check": None, "scheduled_start": None, "candidate_printer_id": None},
    ],
})
STATUS_V2["printers"][0].update({
    "next_job": {
        "id": 12, "name": "Lid", "held": False, "est_minutes": 95, "copies_on_plate": 4,
        "filaments": [{"slot": 2, "color": "#FF0000", "material": "PLA", "grams": 12.5}],
        "plate": {"index": 1, "count": 3},
        "ams_check": {"status": "mismatch", "slots": [
            {"slot": 2, "need": {"color": "#FF0000", "material": "PLA"},
             "loaded": {"color": "#0000FF", "material": "PLA"}, "ok": False}]},
        "scheduled_start": "2026-11-09T03:00:00Z",
    },
    "bench": None,
    "out_of_service": False,
})
STATUS_V2["printers"][1].update({
    "next_job": None,
    "bench": {"state": "paused", "reason": "Paused: possible spaghetti", "since": "2026-11-09T00:40:00Z",
              "pause_cause": "spaghetti"},
    "out_of_service": False,
})


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def status():
    return copy.deepcopy(STATUS)


@pytest.fixture
def status_v2():
    return copy.deepcopy(STATUS_V2)
