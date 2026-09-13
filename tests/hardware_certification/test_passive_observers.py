from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import websocket

from ops.hardware_certification.passive.observers import (
    MOON_QUERY, ObservationError, observe_bambu, observe_elegoo,
    observe_moonraker, observe_prusalink,
)
from ops.hardware_certification.passive.parsers import ParseError, parse_moonraker_sample
from ops.hardware_certification.passive.parsers import (
    parse_bambu_sample, parse_elegoo_sample, parse_prusalink_sample,
)
from modules.printers.telemetry.state import PrinterStatus
from ops.hardware_certification.passive.transports import PassiveWebSocketTransport, ReadOnlyHttpTransport
from ops.hardware_certification.passive.live import _cleanup_preserving_primary
from ops.hardware_certification.simulators import MiniJsonHttpPeer, MiniWebSocketPeer


def test_passive_cleanup_attempts_every_step_and_preserves_primary_interrupt():
    calls: list[str] = []

    def fail(name: str):
        def callback():
            calls.append(name)
            raise RuntimeError(name)
        return callback

    try:
        raise KeyboardInterrupt
    except KeyboardInterrupt:
        _cleanup_preserving_primary(fail("loop_stop"), fail("disconnect"))
        assert sys.exc_info()[0] is KeyboardInterrupt
    assert calls == ["loop_stop", "disconnect"]


def test_passive_cleanup_raises_first_failure_only_after_all_steps():
    calls: list[str] = []

    def first():
        calls.append("close")
        raise RuntimeError("close failed")

    def second():
        calls.append("join")

    with pytest.raises(RuntimeError, match="close failed"):
        _cleanup_preserving_primary(first, second)
    assert calls == ["close", "join"]


def test_bambu_observer_requires_two_v2_samples_and_reports_ams_capability():
    reports = [
        {"print": {"gcode_state": "IDLE", "mc_percent": 0, "ams": {"ams": []}}},
        {"print": {"gcode_state": "RUNNING", "stg_cur": 14, "mc_percent": 25, "ams": {"ams": []}}},
    ]
    ticks = iter((42.0, 42.25))
    summary = observe_bambu(reports, clock=lambda: next(ticks))
    assert len(summary.samples) == 2
    assert summary.samples[-1].state == "printing"
    assert summary.capabilities == ("ams_read", "status")
    assert summary.freshness_seconds == 0.25
    with pytest.raises(ObservationError, match="insufficient_samples"):
        observe_bambu(reports[:1], clock=lambda: 42.0)


def test_moonraker_observer_uses_real_parser_and_two_bounded_queries():
    responses = {
        "/server/info": {"result": {"moonraker_version": "v0.9.3"}},
        "/printer/objects/list": {"result": {"objects": ["print_stats", "heater_bed"]}},
        MOON_QUERY: [
            {"result": {"eventtime": 1.0, "status": {
                "print_stats": {"state": "printing", "filename": "private-job.gcode"},
                "virtual_sdcard": {"progress": 0.5}, "heater_bed": {"temperature": 60, "target": 60},
                "extruder": {"temperature": 205, "target": 210},
            }}},
            {"result": {"eventtime": 1.1, "status": {
                "print_stats": {"state": "printing", "filename": "private-job.gcode"},
                "virtual_sdcard": {"progress": 0.51}, "heater_bed": {"temperature": 60, "target": 60},
                "extruder": {"temperature": 205, "target": 210},
            }}},
        ],
    }
    with MiniJsonHttpPeer(responses) as peer:
        summary = observe_moonraker(ReadOnlyHttpTransport(peer.base_url, "moonraker"))
    assert len(summary.samples) == 2
    assert summary.samples[0].state == "printing"
    assert summary.api_version == "v0.9.3"
    assert summary.capabilities == ("heated_bed", "status")
    assert peer.requests.count(("GET", MOON_QUERY)) == 2


def test_prusalink_observer_combines_status_and_job_without_retaining_raw_payload():
    responses = {
        "/api/version": {
            "api": "2.0", "version": "0.8.1", "printer": "1.3.1",
            "firmware": "6.2.0",
        },
        "/api/v1/status": [
            {"printer": {"state": "PRINTING", "temp_bed": 55, "temp_nozzle": 210}, "telemetry_sequence": 1},
            {"printer": {"state": "PRINTING", "temp_bed": 55, "temp_nozzle": 210}, "telemetry_sequence": 2},
        ],
        "/api/job": [
            {"job": {"id": 71, "file": {"name": "private-job.gcode"}, "progress": 42, "time_printing": 300}},
            {"job": {"id": 71, "file": {"name": "private-job.gcode"}, "progress": 42, "time_printing": 301}},
        ],
    }
    with MiniJsonHttpPeer(responses) as peer:
        summary = observe_prusalink(ReadOnlyHttpTransport(peer.base_url, "prusalink"))
    assert len(summary.samples) == 2
    assert summary.samples[-1].job_id == 71
    assert summary.api_version == "2.0"
    assert summary.firmware_version == "6.2.0"
    assert summary.observed_model_family is None
    assert not hasattr(summary.samples[-1], "raw_data")


def test_prusalink_connection_routes_never_report_version_string_as_model():
    for path in (
        Path("backend/modules/system/routes_setup.py"),
        Path("backend/modules/printers/routes_crud.py"),
    ):
        source = path.read_text(encoding="utf-8")
        assert 'normalize_model_name("prusalink"' not in source


def test_elegoo_observer_correlates_attributes_with_two_unsolicited_status_frames():
    frames = [
        json.dumps({
            "Topic": "sdcp/attributes/ODIN-CERT-FICTIONAL",
            "Attributes": {
                "MachineName": "Centauri Carbon", "ProtocolVersion": "V3.0.0",
                "FirmwareVersion": "V1.0.0",
            },
        }),
        json.dumps({"Topic": "sdcp/status/ODIN-CERT-FICTIONAL", "Status": {"CurrentStatus": [0], "PrintInfo": {"Status": 0}}}),
        json.dumps({"Topic": "sdcp/status/ODIN-CERT-FICTIONAL", "Status": {"CurrentStatus": [1], "PrintInfo": {"Status": 1}}}),
    ]
    with MiniWebSocketPeer(frames) as peer:
        raw = websocket.create_connection(peer.url, timeout=3)
        transport = PassiveWebSocketTransport(raw)
        try:
            summary = observe_elegoo(transport)
        finally:
            transport.close()
    assert [sample.state for sample in summary.samples] == ["idle", "printing"]
    assert summary.observed_model_family == "Centauri Carbon"
    assert summary.firmware_version == "V1.0.0"
    assert summary.api_version == "V3.0.0"


def test_elegoo_observer_rejects_cross_device_attributes_and_missing_model():
    mismatched = iter((
        json.dumps({
            "Topic": "sdcp/attributes/ODIN-CERT-DEVICE-A",
            "Attributes": {
                "MachineName": "Centauri Carbon",
                "FirmwareVersion": "V1.0.0", "ProtocolVersion": "V3.0.0",
            },
        }),
        json.dumps({
            "Topic": "sdcp/status/ODIN-CERT-DEVICE-B",
            "Status": {"CurrentStatus": [0], "PrintInfo": {"Status": 0}},
        }),
    ))
    transport = PassiveWebSocketTransport(type("Socket", (), {
        "recv": lambda self: next(mismatched), "close": lambda self: None,
    })())
    with pytest.raises(ObservationError, match="target_identity_mismatch"):
        observe_elegoo(transport)

    missing_model = iter((
        json.dumps({
            "Topic": "sdcp/attributes/ODIN-CERT-FICTIONAL", "Attributes": {},
        }),
    ))
    transport = PassiveWebSocketTransport(type("Socket", (), {
        "recv": lambda self: next(missing_model), "close": lambda self: None,
    })())
    with pytest.raises(ObservationError, match="model_identity_unavailable"):
        observe_elegoo(transport)


def test_elegoo_and_bambu_observers_reject_excessive_json_nesting():
    nested: object = "leaf"
    for _ in range(40):
        nested = {"next": nested}
    elegoo = PassiveWebSocketTransport(type("Socket", (), {
        "recv": lambda self: json.dumps({"Topic": "sdcp/status/ODIN-CERT-FICTIONAL", "extra": nested}),
        "close": lambda self: None,
    })())
    with pytest.raises(ObservationError, match="malformed_or_unavailable"):
        observe_elegoo(elegoo)
    with pytest.raises(ObservationError, match="malformed_telemetry"):
        observe_bambu([json.dumps({"print": {"gcode_state": "IDLE", "extra": nested}})])

    pathological = '"leaf"'
    for _ in range(2000):
        pathological = '{"next":' + pathological + '}'
    with pytest.raises(ObservationError, match="malformed_telemetry"):
        observe_bambu([pathological])


def test_numeric_range_and_unknown_version_fail_or_redact_safely():
    with pytest.raises(ParseError, match="progress_percent"):
        parse_moonraker_sample({"result": {"status": {
            "print_stats": {"state": "printing"}, "virtual_sdcard": {"progress": 2.0},
        }}})


@pytest.mark.parametrize("protocol", ["bambu", "moonraker", "prusalink", "elegoo"])
def test_protocol_unknown_enum_and_extra_field_policy(protocol):
    if protocol == "bambu":
        with pytest.raises(ParseError):
            parse_bambu_sample(
                {"print": {"gcode_state": "FUTURE_STATE", "future_field": 42}},
                previous=PrinterStatus.initial(), timestamp=1.0,
            )
    elif protocol == "moonraker":
        sample = parse_moonraker_sample({
            "result": {"status": {"print_stats": {"state": "future_state"}, "future_field": 42}},
        })
        assert sample.state == "unknown"
    elif protocol == "prusalink":
        sample = parse_prusalink_sample(
            {"printer": {"state": "future_state"}, "future_field": 42},
            {"job": {"id": 1, "future_field": 42}},
        )
        assert sample.state == "unknown"
    else:
        sample = parse_elegoo_sample({
            "Topic": "sdcp/status/ODIN-CERT-FICTIONAL",
            "Status": {"CurrentStatus": [999], "PrintInfo": {"Status": 999}, "Future": 42},
        })
        assert sample.state == "unknown"


def test_bambu_observer_rejects_repeated_timestamp_and_model_drift():
    repeated = [
        {"print": {"gcode_state": "IDLE", "printer_type": "BL-P001"}},
        {"print": {"gcode_state": "RUNNING", "printer_type": "BL-P001"}},
    ]
    with pytest.raises(ObservationError, match="sample_freshness_invalid"):
        observe_bambu(repeated, clock=lambda: 42.0)

    drifted = [
        {"print": {"gcode_state": "IDLE", "printer_type": "BL-P001"}},
        {"print": {"gcode_state": "RUNNING", "printer_type": "BL-P002"}},
    ]
    ticks = iter((42.0, 42.1))
    with pytest.raises(ObservationError, match="model_family_mismatch"):
        observe_bambu(drifted, clock=lambda: next(ticks))


def test_http_observer_rejects_identical_snapshots_despite_local_elapsed_time():
    responses = {
        "/server/info": {"result": {"moonraker_version": "v1"}},
        "/printer/objects/list": {"result": {"objects": ["print_stats"]}},
        MOON_QUERY: [
            {"result": {"status": {"print_stats": {"state": "standby"}}}},
            {"result": {"status": {"print_stats": {"state": "standby"}}}},
        ],
    }
    with MiniJsonHttpPeer(responses) as peer:
        with pytest.raises(ObservationError, match="sample_freshness_invalid"):
            observe_moonraker(ReadOnlyHttpTransport(peer.base_url, "moonraker"))


@pytest.mark.parametrize("protocol", ["bambu", "elegoo", "moonraker", "prusalink"])
def test_protocol_repeated_payload_cannot_prove_freshness(protocol):
    ticks = iter((1.0, 1.1))
    if protocol == "bambu":
        payload = {"print": {"gcode_state": "IDLE", "printer_type": "BL-P001"}}
        with pytest.raises(ObservationError, match="sample_freshness_invalid"):
            observe_bambu([payload, payload], clock=lambda: next(ticks))
    elif protocol == "elegoo":
        frame = json.dumps({
            "Topic": "sdcp/status/ODIN-CERT-FICTIONAL",
            "Status": {"CurrentStatus": [0], "PrintInfo": {"Status": 0}},
        })
        frames = iter((
            json.dumps({
                "Topic": "sdcp/attributes/ODIN-CERT-FICTIONAL",
                "Attributes": {
                    "MachineName": "Centauri Carbon",
                    "FirmwareVersion": "V1.0.0", "ProtocolVersion": "V3.0.0",
                },
            }),
            frame,
            frame,
        ))
        transport = PassiveWebSocketTransport(type("Socket", (), {
            "recv": lambda self: next(frames), "close": lambda self: None,
        })())
        with pytest.raises(ObservationError, match="sample_freshness_invalid"):
            observe_elegoo(transport, clock=lambda: next(ticks))
    else:
        if protocol == "moonraker":
            responses = {
                "/server/info": {"result": {"moonraker_version": "v1"}},
                "/printer/objects/list": {"result": {"objects": ["print_stats"]}},
                MOON_QUERY: [
                    {"result": {"status": {"print_stats": {"state": "standby"}}}},
                    {"result": {"status": {"print_stats": {"state": "standby"}}}},
                ],
            }
        else:
            responses = {
                "/api/version": {"api": "2.0"},
                "/api/v1/status": [
                    {"printer": {"state": "IDLE"}}, {"printer": {"state": "IDLE"}},
                ],
                "/api/job": [
                    {"job": {"id": 1}}, {"job": {"id": 1}},
                ],
            }
        with MiniJsonHttpPeer(responses) as peer:
            observer = observe_moonraker if protocol == "moonraker" else observe_prusalink
            with pytest.raises(ObservationError, match="sample_freshness_invalid"):
                observer(ReadOnlyHttpTransport(peer.base_url, protocol), clock=lambda: next(ticks))


def test_bambu_and_elegoo_unknown_field_changes_do_not_prove_freshness():
    ticks = iter((1.0, 1.1))
    bambu = [
        {"print": {"gcode_state": "IDLE"}, "future": 1},
        {"print": {"gcode_state": "IDLE"}, "future": 2},
    ]
    with pytest.raises(ObservationError, match="sample_freshness_invalid"):
        observe_bambu(bambu, clock=lambda: next(ticks))

    frames = iter((
        json.dumps({
            "Topic": "sdcp/attributes/ODIN-CERT-FICTIONAL",
            "Attributes": {
                "MachineName": "Centauri Carbon",
                "FirmwareVersion": "V1.0.0", "ProtocolVersion": "V3.0.0",
            },
        }),
        json.dumps({"Topic": "sdcp/status/ODIN-CERT-FICTIONAL", "Status": {
            "CurrentStatus": [0], "PrintInfo": {"Status": 0}, "Future": 1,
        }}),
        json.dumps({"Topic": "sdcp/status/ODIN-CERT-FICTIONAL", "Status": {
            "CurrentStatus": [0], "PrintInfo": {"Status": 0}, "Future": 2,
        }}),
    ))
    transport = PassiveWebSocketTransport(type("Socket", (), {
        "recv": lambda self: next(frames), "close": lambda self: None,
    })())
    ticks = iter((1.0, 1.1))
    with pytest.raises(ObservationError, match="sample_freshness_invalid"):
        observe_elegoo(transport, clock=lambda: next(ticks))


def test_http_unknown_field_changes_do_not_prove_freshness():
    moon = {
        "/server/info": {"result": {"moonraker_version": "v1"}},
        "/printer/objects/list": {"result": {"objects": ["print_stats"]}},
        MOON_QUERY: [
            {"result": {"status": {"print_stats": {"state": "standby"}, "future": 1}}},
            {"result": {"status": {"print_stats": {"state": "standby"}, "future": 2}}},
        ],
    }
    with MiniJsonHttpPeer(moon) as peer:
        with pytest.raises(ObservationError, match="sample_freshness_invalid"):
            observe_moonraker(ReadOnlyHttpTransport(peer.base_url, "moonraker"))

    prusa = {
        "/api/version": {"api": "2.0"},
        "/api/v1/status": [
            {"printer": {"state": "IDLE"}, "future": 1},
            {"printer": {"state": "IDLE"}, "future": 2},
        ],
        "/api/job": [{"job": {"id": 1}}, {"job": {"id": 1}}],
    }
    with MiniJsonHttpPeer(prusa) as peer:
        with pytest.raises(ObservationError, match="sample_freshness_invalid"):
            observe_prusalink(ReadOnlyHttpTransport(peer.base_url, "prusalink"))


def test_protocol_sequence_counters_must_be_strictly_monotonic():
    ticks = iter((1.0, 1.1))
    reports = [
        {"print": {"gcode_state": "IDLE", "msg": 9}},
        {"print": {"gcode_state": "RUNNING", "msg": 8}},
    ]
    with pytest.raises(ObservationError, match="sample_freshness_invalid"):
        observe_bambu(reports, clock=lambda: next(ticks))

    moon = {
        "/server/info": {"result": {"moonraker_version": "v1"}},
        "/printer/objects/list": {"result": {"objects": ["print_stats"]}},
        MOON_QUERY: [
            {"result": {"eventtime": 2.0, "status": {"print_stats": {"state": "standby"}}}},
            {"result": {"eventtime": 1.0, "status": {"print_stats": {"state": "printing"}}}},
        ],
    }
    with MiniJsonHttpPeer(moon) as peer:
        with pytest.raises(ObservationError, match="sample_freshness_invalid"):
            observe_moonraker(ReadOnlyHttpTransport(peer.base_url, "moonraker"))


def test_advancing_native_counter_proves_freshness_for_stable_idle_state():
    moon = {
        "/server/info": {"result": {"moonraker_version": "v1"}},
        "/printer/objects/list": {"result": {"objects": ["print_stats"]}},
        MOON_QUERY: [
            {"result": {"eventtime": 1.0, "status": {"print_stats": {"state": "standby"}}}},
            {"result": {"eventtime": 1.1, "status": {"print_stats": {"state": "standby"}}}},
        ],
    }
    with MiniJsonHttpPeer(moon) as peer:
        assert observe_moonraker(
            ReadOnlyHttpTransport(peer.base_url, "moonraker")
        ).freshness_seconds > 0

    prusa = {
        "/api/version": {"api": "2.0"},
        "/api/v1/status": [
            {"printer": {"state": "IDLE"}, "telemetry_sequence": 10},
            {"printer": {"state": "IDLE"}, "telemetry_sequence": 11},
        ],
        "/api/job": [{"job": {"id": 1}}, {"job": {"id": 1}}],
    }
    with MiniJsonHttpPeer(prusa) as peer:
        assert observe_prusalink(
            ReadOnlyHttpTransport(peer.base_url, "prusalink")
        ).freshness_seconds > 0
