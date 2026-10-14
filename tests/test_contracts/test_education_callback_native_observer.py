"""Actual caught delivery failures remain RED in native observer receipts."""
import ast
from pathlib import Path
from types import SimpleNamespace
import pytest


def observer_functions():
    path=Path(__file__).resolve().parents[2]/'ops/database_parity/education_callback_activation_native.py'
    tree=ast.parse(path.read_text())
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('record_observer','assert_observers')]
    assert len(functions)==2
    namespace={}
    exec(compile(ast.Module(body=functions,type_ignores=[]),str(path),'exec'),namespace)
    return namespace['record_observer'],namespace['assert_observers']


@pytest.mark.parametrize('state',['completed','running','lookup_fault'])
def test_real_publisher_caught_observer_result_is_asserted_outside(monkeypatch,state):
    from core import event_bus
    from modules.printers.committed_terminal_events import publish_committed_completion
    record,assert_records=observer_functions();records=[];attempts=[]
    def read_state(event):
        if state=='lookup_fault':raise RuntimeError('Owned fresh observer fault')
        return state
    def collect(event):
        attempts.append(event)
        record(records,event,read_state)
    monkeypatch.setattr(event_bus,'get_event_bus',lambda:SimpleNamespace(publish=collect))
    delivered=publish_committed_completion(9,11,13)
    assert len(attempts)==len(records)==1
    assert records[0]['state']==(None if state=='lookup_fault' else state)
    if state=='completed':
        assert delivered and records[0]['success'] and records[0]['error'] is None
        assert_records(records)
    else:
        assert delivered is False and records[0]['success'] is False
        assert records[0]['error']==('RuntimeError' if state=='lookup_fault' else 'AssertionError')
        with pytest.raises(AssertionError,match='Recorded observer timing failure'):
            assert_records(records)
