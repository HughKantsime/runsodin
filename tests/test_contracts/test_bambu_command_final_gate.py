"""Final dispatch proof runs after command connection and before publication."""
from types import SimpleNamespace
import pytest
from modules.printers.telemetry.bambu import session


@pytest.mark.parametrize('decision', ['accept', 'refuse', 'exception'])
def test_final_gate_is_after_connection_and_never_forwarded(monkeypatch, decision):
    events=[]
    def check():
        assert events == ['connect']
        events.append('check')
        if decision == 'exception': raise ValueError('synthetic final refusal')
        return decision == 'accept'
    def started(name, **options):
        events.append('start')
        assert options == {'ams_mapping':[0]}
        return True
    monkeypatch.setattr(session,'BambuCommandAdapter',lambda config:SimpleNamespace(
        start=lambda:events.append('connect'), is_connected=lambda:True,
        start_print=started, stop=lambda:events.append('cleanup')))
    result=session.run_command(SimpleNamespace(), 'start_print', 'synthetic.3mf',
        ams_mapping=[0], before_command=check)
    assert result is (decision == 'accept')
    assert events == (['connect','check','start','cleanup'] if decision == 'accept'
                      else ['connect','check','cleanup'])


def test_gate_cannot_be_attached_to_an_unrelated_command(monkeypatch):
    events=[]
    monkeypatch.setattr(session,'BambuCommandAdapter',lambda config:SimpleNamespace(
        start=lambda:None,is_connected=lambda:True,pause_print=lambda:events.append('pause'),
        stop=lambda:events.append('cleanup')))
    assert not session.run_command(SimpleNamespace(), 'pause_print', before_command=lambda:True)
    assert events == ['cleanup']
