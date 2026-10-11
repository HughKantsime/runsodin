"""Legacy environmental parsing must tolerate incomplete device reports."""
import pytest
from modules.printers.monitors.mqtt_telemetry import parse_ams_env


def test_environment_preserves_units_and_zero_temperature():
    assert parse_ams_env({'ams': [{'humidity': '2', 'temp': '0'}, {'humidity': 5, 'temp': 24.5}]}) == [
        {'unit_idx': 0, 'humidity': 2, 'temperature': 0.0},
        {'unit_idx': 1, 'humidity': 5, 'temperature': 24.5},
    ]


def test_environment_skips_empty_and_malformed_unit_without_reindexing():
    assert parse_ams_env({'ams': [{}, None, {'humidity':'bad', 'temp':'21'}]}) == [
        {'unit_idx': 2, 'humidity': None, 'temperature': 21.0},
    ]


@pytest.mark.parametrize('payload', [None, {}, [], {'ams': None}, {'ams': 42}])
def test_environment_missing_or_invalid_collection_is_empty(payload):
    assert parse_ams_env(payload) == []


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', float('nan'), float('inf'), True])
def test_environment_invalid_temperature_keeps_valid_humidity(value):
    assert parse_ams_env({'ams': [{'humidity':'3', 'temp':value}]}) == [
        {'unit_idx':0, 'humidity':3, 'temperature':None},
    ]


@pytest.mark.parametrize('value', [float('inf'), float('nan'), True, 1.5, 0, -1, 6, 10**100, '0', '6'])
def test_environment_invalid_humidity_keeps_valid_temperature(value):
    assert parse_ams_env({'ams': [{'humidity':value, 'temp':25}]}) == [
        {'unit_idx':0, 'humidity':None, 'temperature':25.0},
    ]


@pytest.mark.parametrize('unit', [{}, {'temp':'bad'}, {'humidity':'bad'}, {'temp':'NaN','humidity':True}])
def test_environment_without_any_valid_measurement_is_omitted(unit):
    assert parse_ams_env({'ams':[unit]}) == []


@pytest.mark.parametrize('humidity', ['1', '5', 1, 5, 3.0])
def test_environment_integer_humidity_preserved_without_inventing_temperature(humidity):
    assert parse_ams_env({'ams':[{'humidity':humidity}]}) == [
        {'unit_idx':0,'humidity':int(humidity),'temperature':None},
    ]
