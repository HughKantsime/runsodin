"""Guarded lifecycle preparation; substituted devices and timer, no dispatch."""
from types import SimpleNamespace
import pytest


@pytest.fixture
def preparation(monkeypatch):
    from modules.printers import smart_plug as module
    config = {'type':'tasmota','host':'127.0.0.1:1','entity_id':'synthetic',
              'auth_token':'synthetic private marker','auto_on':True,'auto_off':False}
    calls = []
    timer = SimpleNamespace(cancel=lambda: calls.append('cancel'))
    monkeypatch.setattr(module, '_cooldown_timers', {599:timer})
    monkeypatch.setattr(module, 'get_plug_config', lambda identifier: dict(config) if config else None)
    monkeypatch.setattr(module, '_update_power_state', lambda *args: None)
    return module, config, calls


@pytest.mark.parametrize('mode', ['absent','disabled'])
def test_preparation_cancels_off_without_power_for_disabled_config(preparation, monkeypatch, mode):
    module, config, calls = preparation
    if mode == 'absent': config.clear()
    else: config['auto_on'] = False
    monkeypatch.setattr(module, 'power_on', lambda *args, **kwargs: pytest.fail('Disabled power must not send'))
    result = module.prepare_dispatch_power(599)
    assert result.accepted and not result.required
    assert calls == ['cancel'] and 599 not in module._cooldown_timers


@pytest.mark.parametrize('outcome', [True, False, None, 'accepted','unconfirmed','rejected'])
def test_preparation_explicit_on_with_truthful_receipt(preparation, monkeypatch, outcome):
    module, config, calls = preparation
    config['type'] = 'mqtt' if isinstance(outcome,str) else 'tasmota'
    def device(*args):
        calls.append(args)
        return module.MqttPowerReceipt(outcome) if isinstance(outcome,str) else outcome
    monkeypatch.setattr(module, 'mqtt_power', device)
    monkeypatch.setattr(module, 'tasmota_power', device)
    result = module.prepare_dispatch_power(599)
    assert result.accepted == (outcome is True or outcome == 'accepted')
    assert result.required
    assert calls == ['cancel', ('synthetic' if config['type']=='mqtt' else config['host'], 'ON')]
    assert 'synthetic private marker' not in repr(result)


@pytest.mark.parametrize('change', ['host','disabled','absent'])
def test_configuration_changed_during_power_command_stops_preparation(preparation, monkeypatch, change):
    module, config, calls = preparation
    def device(*args):
        if change == 'host': config['host'] = '127.0.0.2:1'
        elif change == 'disabled': config['auto_on'] = False
        else: config.clear()
        return True
    monkeypatch.setattr(module, 'tasmota_power', device)
    result = module.prepare_dispatch_power(599)
    assert not result.accepted and result.required
    assert 'configuration changed' in result.error.lower()


def test_expected_identity_refused_before_device_if_config_replaced(preparation, monkeypatch):
    module, config, calls = preparation
    identity = module._plug_identity(config)
    config['host'] = '127.0.0.2:1'
    monkeypatch.setattr(module, 'tasmota_power', lambda *args: pytest.fail('Changed destination must not send'))
    assert module.power_on(599, expected_config=identity) is None


def test_preparation_failure_generic_and_no_retry(preparation, monkeypatch, caplog):
    module, config, calls = preparation
    def device(*args):
        calls.append('send')
        raise RuntimeError('synthetic private marker')
    monkeypatch.setattr(module, 'tasmota_power', device)
    result = module.prepare_dispatch_power(599)
    assert not result.accepted and result.required
    assert calls == ['cancel','send']
    assert 'synthetic private marker' not in result.error + caplog.text
