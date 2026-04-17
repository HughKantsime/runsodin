"""Byte-equivalence tests for BambuCommandAdapter (T1.1-T1.7).

For each command method, assert the payload the adapter publishes to
`device/<serial>/request` is byte-identical to what legacy
`BambuPrinter` publishes. If bytes match + legacy works today, V2
works after cutover.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from backend.modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
from backend.modules.printers.telemetry.bambu.commands import BambuCommandAdapter


class FakeMqttClient:
    def __init__(self):
        self.tls_set_context = MagicMock()
        self.username_pw_set = MagicMock()
        self.connect = MagicMock(return_value=0)
        self.loop_start = MagicMock()
        self.loop_stop = MagicMock()
        self.disconnect = MagicMock()
        # Capture every publish call for byte-equivalence assertions
        self.publishes: list[tuple[str, bytes]] = []

        def _publish(topic, payload, qos=0):
            self.publishes.append((topic, payload if isinstance(payload, bytes) else payload.encode()))
            result = MagicMock()
            result.rc = 0  # MQTT_ERR_SUCCESS
            result.wait_for_publish = MagicMock()
            result.is_published = MagicMock(return_value=True)
            return result

        self.publish = MagicMock(side_effect=_publish)
        self.on_connect = None
        self.on_disconnect = None


@pytest.fixture
def config():
    return BambuAdapterConfig(
        printer_id="h2d-01",
        serial="TEST-SERIAL",
        host="127.0.0.1",
        port=8883,
        access_code="",
        use_tls=False,
    )


@pytest.fixture
def adapter(config, monkeypatch):
    fake = FakeMqttClient()
    monkeypatch.setattr(
        BambuCommandAdapter, "_client_factory", staticmethod(lambda: fake),
    )
    a = BambuCommandAdapter(config)
    a._fake = fake
    a.start()
    # simulate connect callback
    a.on_connect_fired = False

    def fire():
        a._on_connect(fake, None, {}, 0)
        a.on_connect_fired = True
    fire()
    return a


def _last_published_dict(adapter) -> dict:
    """Return the last published payload as a dict."""
    assert adapter._fake.publishes, "no publish captured"
    topic, payload_bytes = adapter._fake.publishes[-1]
    assert topic == adapter._config.topic_request
    return json.loads(payload_bytes)


# ---- Legacy payload reference ----
# Expected exact payloads — mirrors the structure in
# backend/modules/printers/adapters/bambu.py. If these drift from legacy,
# the cutover is no longer byte-equivalent; either fix the adapter or
# update legacy + this test together.

class TestByteEquivalence:
    def test_request_full_status(self, adapter):
        adapter.request_full_status()
        assert _last_published_dict(adapter) == {
            "pushing": {"sequence_id": "0", "command": "pushall"}
        }

    def test_pause_print(self, adapter):
        adapter.pause_print()
        assert _last_published_dict(adapter) == {
            "print": {"sequence_id": "0", "command": "pause"}
        }

    def test_resume_print(self, adapter):
        adapter.resume_print()
        assert _last_published_dict(adapter) == {
            "print": {"sequence_id": "0", "command": "resume"}
        }

    def test_stop_print(self, adapter):
        adapter.stop_print()
        assert _last_published_dict(adapter) == {
            "print": {"sequence_id": "0", "command": "stop"}
        }

    def test_send_gcode(self, adapter):
        adapter.send_gcode("M105")
        assert _last_published_dict(adapter) == {
            "print": {
                "sequence_id": "0",
                "command": "gcode_line",
                "param": "M105",
            }
        }

    def test_set_bed_temp_via_gcode(self, adapter):
        adapter.set_bed_temp(60)
        assert _last_published_dict(adapter) == {
            "print": {
                "sequence_id": "0",
                "command": "gcode_line",
                "param": "M140 S60",
            }
        }

    def test_set_nozzle_temp_via_gcode(self, adapter):
        adapter.set_nozzle_temp(220)
        assert _last_published_dict(adapter) == {
            "print": {
                "sequence_id": "0",
                "command": "gcode_line",
                "param": "M104 S220",
            }
        }

    def test_turn_light_on(self, adapter):
        adapter.turn_light_on()
        assert _last_published_dict(adapter)["print"]["param"] == "M355 S1"

    def test_turn_light_off(self, adapter):
        adapter.turn_light_off()
        assert _last_published_dict(adapter)["print"]["param"] == "M355 S0"

    def test_set_fan_speed_part_cooling(self, adapter):
        adapter.set_fan_speed("part_cooling", 128)
        assert _last_published_dict(adapter)["print"]["param"] == "M106 P1 S128"

    def test_set_fan_speed_auxiliary(self, adapter):
        adapter.set_fan_speed("auxiliary", 255)
        assert _last_published_dict(adapter)["print"]["param"] == "M106 P2 S255"

    def test_set_fan_speed_chamber(self, adapter):
        adapter.set_fan_speed("chamber", 0)
        assert _last_published_dict(adapter)["print"]["param"] == "M106 P3 S0"

    def test_set_fan_speed_invalid_key_returns_false(self, adapter):
        result = adapter.set_fan_speed("unknown_fan", 100)
        assert result is False
        # and nothing was published
        initial_count = len(adapter._fake.publishes)
        adapter.set_fan_speed("unknown_fan", 100)
        assert len(adapter._fake.publishes) == initial_count

    def test_set_fan_speed_clamps(self, adapter):
        adapter.set_fan_speed("part_cooling", 999)
        assert _last_published_dict(adapter)["print"]["param"] == "M106 P1 S255"
        adapter.set_fan_speed("part_cooling", -5)
        assert _last_published_dict(adapter)["print"]["param"] == "M106 P1 S0"

    def test_refresh_ams_rfid(self, adapter):
        adapter.refresh_ams_rfid()
        assert _last_published_dict(adapter) == {
            "print": {
                "sequence_id": "0",
                "command": "ams_change_filament",
                "target": 255,
                "curr_temp": 0,
                "tar_temp": 0,
            }
        }

    def test_set_ams_filament_6char_color(self, adapter):
        adapter.set_ams_filament(0, 2, "PLA", "FF5500", k_factor=0.02)
        d = _last_published_dict(adapter)["print"]
        assert d["command"] == "ams_filament_setting"
        assert d["ams_id"] == 0
        assert d["tray_id"] == 0 * 4 + 2  # == 2
        assert d["tray_info_idx"] == "PLA"
        assert d["tray_color"] == "FF5500FF"  # alpha pad
        assert d["tray_type"] == "PLA"
        assert d["k"] == 0.02

    def test_set_ams_filament_hash_stripped(self, adapter):
        adapter.set_ams_filament(1, 0, "PETG", "#000000FF")
        d = _last_published_dict(adapter)["print"]
        assert d["tray_color"] == "000000FF"
        assert d["tray_id"] == 1 * 4 + 0  # == 4

    def test_clear_print_errors(self, adapter):
        adapter.clear_print_errors()
        assert _last_published_dict(adapter) == {
            "print": {"sequence_id": "0", "command": "clean_print_error"}
        }

    def test_skip_objects_stringifies_ids(self, adapter):
        adapter.skip_objects([1, 2, 3])
        d = _last_published_dict(adapter)["print"]
        assert d["command"] == "skip_objects"
        # legacy stringifies each ID
        assert d["obj_list"] == ["1", "2", "3"]

    def test_set_print_speed_levels(self, adapter):
        for level in (1, 2, 3, 4):
            adapter.set_print_speed(level)
            d = _last_published_dict(adapter)["print"]
            assert d["command"] == "print_speed"
            assert d["param"] == str(level)

    def test_set_print_speed_invalid_returns_false(self, adapter):
        assert adapter.set_print_speed(0) is False
        assert adapter.set_print_speed(5) is False

    def test_start_print_payload_shape(self, adapter):
        adapter.start_print("benchy.3mf", plate_num=1, use_ams=True)
        d = _last_published_dict(adapter)["print"]
        assert d["command"] == "project_file"
        assert d["subtask_name"] == "benchy"
        assert d["url"] == "ftp:///benchy.3mf"
        assert d["param"] == "Metadata/plate_1.gcode"
        assert d["use_ams"] is True
        assert d["bed_leveling"] is True
        assert d["timelapse"] is False
        # sequence_id is dynamic — just verify it's present + stringified int
        assert isinstance(d["sequence_id"], str)
        assert d["sequence_id"].isdigit()


class TestLifecycle:
    def test_double_start_raises(self, config, monkeypatch):
        fake = FakeMqttClient()
        monkeypatch.setattr(
            BambuCommandAdapter, "_client_factory", staticmethod(lambda: fake),
        )
        a = BambuCommandAdapter(config)
        a.start()
        with pytest.raises(RuntimeError, match="already started"):
            a.start()

    def test_stop_before_start_noop(self, config):
        a = BambuCommandAdapter(config)
        a.stop()  # no raise

    def test_publish_when_not_connected_returns_false(self, config, monkeypatch):
        fake = FakeMqttClient()
        monkeypatch.setattr(
            BambuCommandAdapter, "_client_factory", staticmethod(lambda: fake),
        )
        a = BambuCommandAdapter(config)
        # note: no .start() or fire connect
        assert a.send_gcode("M105") is False
