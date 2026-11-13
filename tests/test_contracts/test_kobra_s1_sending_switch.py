"""Kobra S1 sending is experimental: off unless the server operator sets ODIN_KOBRA_S1_SENDING."""
import pytest

from modules.printers import dispatch_support as support

S1 = {'api_type': 'moonraker', 'model': 'Kobra S1', 'machine_type': None}


def test_off_by_default_the_s1_is_refused_like_before_the_feature(monkeypatch):
    assert support.s1_sending_enabled() is False
    reason = support.dispatch_unsupported_reason(S1)
    assert reason.startswith('printer_dispatch_unsupported: ') and 'turned off' in reason
    assert support.automatic_dispatch_unsupported_reason(S1) == reason
    assert support.is_kobra_s1(S1)            # still recognised: monitoring and ACE do not depend on the switch


@pytest.mark.parametrize('value, on', [('1', True), ('true', True), (' YES ', True), ('0', False), ('', False), ('on', False)])
def test_only_an_explicit_yes_turns_it_on(monkeypatch, value, on):
    monkeypatch.setenv('ODIN_KOBRA_S1_SENDING', value)
    assert support.s1_sending_enabled() is on
    assert (support.dispatch_unsupported_reason(S1) is None) is on
    if on:   # explicit start only, never the scheduler
        assert support.automatic_dispatch_unsupported_reason(S1).startswith('s1_explicit_dispatch_required')


def test_the_sender_itself_refuses_when_off_before_touching_the_printer(monkeypatch):
    from modules.printers import s1_dispatch
    from modules.printers.adapters import moonraker
    monkeypatch.setattr(moonraker, 'MoonrakerPrinter', lambda *a, **k: pytest.fail('printer contacted'))
    ok, message = s1_dispatch.dispatch(1, '/nonexistent', 'x.gcode', {'ip': '127.0.0.1', 'port': 1}, {}, lambda: True)
    assert ok is False and 'turned off' in message


def test_the_claimed_path_refuses_before_any_claim_when_off():
    from modules.printers.dispatch_orchestration import _dispatch_claimed
    ok, message = _dispatch_claimed({'id': 1, 'printer_id': 4}, S1, '/nonexistent', 'x.gcode', '0' * 64, None, None)
    assert ok is False and 'turned off' in message


def test_other_kobra_models_are_unaffected_by_the_switch(monkeypatch):
    other = {'api_type': 'moonraker', 'model': 'Kobra 3', 'machine_type': None}
    before = support.dispatch_unsupported_reason(other)
    monkeypatch.setenv('ODIN_KOBRA_S1_SENDING', '1')
    assert support.dispatch_unsupported_reason(other) == before and before is not None
