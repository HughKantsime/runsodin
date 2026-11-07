"""Start-next audit atomicity, recipe identity, and controlled member regression."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from sqlalchemy import text
from modules.jobs import completion_summary as service
from modules.printers import start_next
from tests.test_contracts.test_completion_summary import queue, advice, request, recipe
from tests.test_contracts.test_private_attempt_valuation_route import review_db


def test_start_next_dispatch_audit_preserves_request_actor(queue,review_db,monkeypatch):
    from core.audit_actor import RequestAuditScope,register_request_session,bind_verified_actor,_SCOPE_KEY
    from modules.printers import dispatch
    user={'id':3,'username':'admin','role':'admin','_auth_kind':'session_jwt'}
    scope=RequestAuditScope('127.0.0.1')
    register_request_session(review_db,SimpleNamespace(scope={_SCOPE_KEY:scope}))
    bind_verified_actor(review_db,user)
    monkeypatch.setattr(dispatch,'dispatch_job',lambda *args:(True,'Synthetic accepted'))
    try:
        assert start_next.start(review_db,queue,user,request(advice(review_db,queue)))['success']
        assert review_db.execute(text("SELECT actor_user_id,actor_auth_kind FROM audit_logs WHERE action='job.dispatched' AND entity_id=202")).one()==(3,'session_jwt')
    finally:scope.clear()


def test_start_next_audit_failure_rolls_back_result(queue, review_db, monkeypatch):
    """Dispatch result and its audit share one commit, even if audit staging fails."""
    from core import audit
    from modules.printers import dispatch
    user = {'id': 3, 'role': 'admin'}
    data = request(advice(review_db, queue))
    monkeypatch.setattr(dispatch, 'dispatch_job', lambda *args: (True, 'Synthetic accepted'))

    def reject_audit(*args, **kwargs):
        raise RuntimeError('Synthetic audit failure')

    monkeypatch.setattr(audit, 'log_audit', reject_audit)
    with pytest.raises(RuntimeError, match='Synthetic audit failure'):
        start_next.start(review_db, queue, user, data)
    record = start_next.load(review_db, start_next.request_key(data.request_id, user))
    assert record['result']['state'] == 'preparing'
    assert not record['result']['success']
    assert review_db.execute(text("SELECT count(*) FROM audit_logs WHERE action='job.dispatched' AND entity_id=202")).scalar_one() == 0


@pytest.mark.parametrize('version', [None, 1])
def test_legacy_recipe_never_matches_a_stable_key_by_coincidence(queue, review_db, version):
    spec = recipe(review_db, 202)
    spec['version'] = version
    review_db.execute(service.batches.recipes.update().values(definition=json.dumps(spec)))
    assert not service.matching_recipes(review_db, 0, spec['payload'])


def test_missing_recipe_configuration_cannot_match_unknown_evidence(queue, review_db):
    spec = recipe(review_db, 202)
    spec['payload']['configuration_key'] = None
    review_db.execute(service.batches.recipes.update().values(definition=json.dumps(spec)))
    assert not service.matching_recipes(review_db, 0, spec['payload'])


def test_recipe_advice_identity_is_stable_and_read_only(review_db, monkeypatch):
    from modules.printers import dispatch
    from tests.test_contracts.test_dispatch_identity_behavior import creds
    current = creds(active_materials=['PLA'], filament_slots=[[1, 'PLA'], [4, 'PLA']])
    monkeypatch.setattr(dispatch, '_get_printer_info', lambda *a, **kw: current)
    stable = service.recipe_configuration_key(review_db, 9)
    volatile = service.configuration_key(review_db, 9)
    assert stable and volatile and stable != volatile
    current.update(active_materials=['PLA', 'PETG'], filament_slots=[[1, 'PLA'], [4, 'PETG']])
    assert service.recipe_configuration_key(review_db, 9) == stable
    assert service.configuration_key(review_db, 9) != volatile
    current['nozzle_diameter'] = .6
    assert service.recipe_configuration_key(review_db, 9) != stable
    assert not review_db.new and not review_db.dirty


def test_controlled_member_can_start_the_visible_suggestion(queue,review_db,monkeypatch):
    user={'id':1,'role':'admin','group_id':None}
    review_db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Queue audience')"))
    review_db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,1)'))
    review_db.execute(text("INSERT INTO controlled_files(file_id,label,audience_id,printer_tag,retention_days,reason,marked_at) VALUES(201,'CUI',1,'Secure',7,'fixture',CURRENT_TIMESTAMP)"))
    review_db.commit()
    suggestion=advice(review_db,queue,actor=user)
    assert suggestion['job_id']==202
    assert advice(review_db,queue,actor={'id':3,'role':'admin'})['job_id'] is None
    from modules.printers import dispatch
    send=Mock(return_value=(False,'owned adapter refusal'))
    monkeypatch.setattr(dispatch,'dispatch_job',send)
    result=start_next.start(review_db,queue,user,request(suggestion))
    assert result['state']=='refused' and send.call_count==1
