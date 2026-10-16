"""Actual command serialization through the existing isolated MQTT client fixture."""
import json
import pytest


@pytest.fixture(autouse=True)
def _loopback_device_peers(monkeypatch):
    # Brokers/peers in this file run on loopback, which the production device
    # destination policy blocks; exempt exactly 127.0.0.1 and ::1 here (also in
    # spawned workers). test_cert_A_destination_policy covers the policy.
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)
from tests.test_telemetry.test_telemetry_command_adapter import adapter, config


@pytest.mark.parametrize('ams_id,slot_id', [(0,0),(1,3),(2,2),(3,3)])
@pytest.mark.parametrize('color,expected', [('#12ab34','12AB34FF'),('12ab3480','12AB3480')])
def test_ams_command_preserves_unit_slot_material_alpha_and_pressure_advance(adapter, ams_id, slot_id, color, expected):
    assert adapter.set_ams_filament(ams_id, slot_id, 'PETG', color, 0.028) is True
    topic, encoded = adapter._fake.publishes[-1]
    assert topic == 'device/TEST-SERIAL/request'
    assert json.loads(encoded) == {'print': {
        'sequence_id':'0', 'command':'ams_filament_setting', 'ams_id':ams_id,
        'tray_id':ams_id * 4 + slot_id, 'tray_info_idx':'PETG', 'tray_color':expected,
        'nozzle_temp_min':190, 'nozzle_temp_max':240, 'tray_type':'PETG', 'setting_id':'', 'k':0.028,
    }}


def test_rfid_refresh_requests_no_filament_switch(adapter):
    assert adapter.refresh_ams_rfid() is True
    topic, encoded = adapter._fake.publishes[-1]
    assert topic == 'device/TEST-SERIAL/request'
    assert json.loads(encoded) == {'print': {'sequence_id':'0', 'command':'ams_change_filament', 'target':255, 'curr_temp':0, 'tar_temp':0}}
