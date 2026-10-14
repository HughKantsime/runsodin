"""Core mechanics and existing policy delegation preserve real admission guards."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from tests.test_contracts.test_queue_creation_provider_boundary import queue_api, _facts
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_archive_reprint_behavior import reprint_client


def test_public_payload_exports_are_same_objects():
    from core import print_payload_snapshot as core
    from modules.printers import dispatch_payload_snapshot as old
    for name in ('PayloadUnavailable','PayloadSnapshot','hash_regular_payload','stage_payload'):
        assert getattr(core,name) is getattr(old,name)
    assert old.MAX_PAYLOAD_BYTES == core.MAX_PAYLOAD_BYTES == 100*1024*1024


def test_exact_policy_session_identifier_error_and_no_null_lookup(review_db,monkeypatch):
    from core.registry import registry
    from core.interfaces.education_policy import assert_generic_model_reference_allowed
    from modules.organizations.services import EducationPolicyService
    service=EducationPolicyService();actual=service.assert_generic_model_reference_allowed;observed=[]
    def guard(db,identifier):
        observed.append((db,identifier));return actual(db,identifier)
    monkeypatch.setattr(service,'assert_generic_model_reference_allowed',guard)
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',service)
    before=_facts(review_db)
    assert_generic_model_reference_allowed(review_db,99999)
    assert observed==[(review_db,99999)] and review_db.in_transaction()
    with pytest.raises(HTTPException) as refused:assert_generic_model_reference_allowed(review_db,12)
    assert refused.value.status_code==409 and observed[-1]==(review_db,12)
    review_db.rollback();assert _facts(review_db)==before
    monkeypatch.setattr(registry,'get_provider',lambda _:pytest.fail('NULL model consulted provider'))
    assert_generic_model_reference_allowed(review_db,None)


@pytest.mark.parametrize('mode',['missing','invalid'])
@pytest.mark.parametrize('endpoint',['/api/models/501/schedule-options','/api/models/501/schedule'])
def test_missing_or_invalid_guard_refuses_actual_http_before_mutation(queue_api,review_db,monkeypatch,mode,endpoint):
    from core.registry import registry
    original = registry.get_provider
    policy = None if mode=='missing' else SimpleNamespace(assert_generic_model_reference_allowed=None)
    monkeypatch.setattr(registry, 'get_provider', lambda name: policy if name=='EducationPolicyProvider' else original(name))
    before=_facts(review_db);client,auth,_=queue_api
    response=client.get(endpoint,headers=auth()) if endpoint.endswith('options') else client.post(endpoint,headers=auth(),json={})
    assert response.status_code==503,response.text
    assert response.json()['detail']=='Education policy unavailable'
    assert _facts(review_db)==before


@pytest.mark.parametrize('association',['model','file_alias'])
@pytest.mark.parametrize('endpoint',['/api/models/501/schedule-options','/api/models/501/schedule'])
def test_protected_education_source_cannot_cross_core_boundary(queue_api,review_db,association,endpoint):
    if association=='model':review_db.execute(text('UPDATE education_submissions SET model_id=501 WHERE id=14'))
    else:review_db.execute(text('UPDATE models SET print_file_id=11 WHERE id=501'))
    review_db.commit();before=_facts(review_db);client,auth,_=queue_api
    response=client.get(endpoint,headers=auth()) if endpoint.endswith('options') else client.post(endpoint,headers=auth(),json={})
    assert response.status_code==409,response.text
    assert _facts(review_db)==before
