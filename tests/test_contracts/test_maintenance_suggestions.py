"""Maintenance exclusions use state and stable refusal codes."""
import pytest
from modules.printers import start_next, dispatch
from modules.jobs import completion_summary as service
from modules.printers.routes_maintenance_state import set_maintenance, MaintenanceState
from tests.test_contracts.test_completion_summary import queue, review_db, request, advice


@pytest.mark.parametrize('state', ['hold', 'warn'])
def test_suggestions_respect_maintenance(queue, review_db, state):
    set_maintenance(MaintenanceState(state=state,note='Fan'),queue.id,review_db,{'id':3,'role':'admin'})
    result=service.next_suggestion(review_db,queue)
    assert result['job_id'] == (None if state == 'hold' else 202)
    if state == 'hold': assert result['fingerprint'] is None


def test_start_next_does_not_infer_code_from_message(queue, review_db, monkeypatch):
    monkeypatch.setattr(dispatch,'dispatch_job',lambda *a:(False,'Diagnostic mentions printer_maintenance_hold'))
    result=start_next.start(review_db,queue,{'id':3,'role':'admin'},request(advice(review_db,queue)))
    assert 'code' not in result
