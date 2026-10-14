"""Real Education scoped-token producer/consumer boundaries."""
from fastapi import Depends
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db,interactive_headers


def test_real_education_scoped_token_authenticates(token_client,monkeypatch):
    from modules.organizations import education_access
    monkeypatch.setattr(education_access,'require_education_license',lambda:None)
    client,issue=token_client
    @client.app.post('/synthetic-education-write')
    def protected(principal=Depends(education_access.require_education_principal(write=True))):
        return {'authorized':True}
    response=client.post('/synthetic-education-write',headers=issue(['write:education']))
    assert response.status_code==200
    assert response.json()=={'authorized':True}


import pytest

@pytest.mark.parametrize('scope', ['read:education','write:printers'])
def test_readonly_or_unrelated_scope_cannot_write_education(token_client,monkeypatch,scope):
    from modules.organizations import education_access
    monkeypatch.setattr(education_access,'require_education_license',lambda:None)
    client,issue=token_client
    @client.app.post('/synthetic-education-write-refusal')
    def protected(principal=Depends(education_access.require_education_principal(write=True))):
        return {'authorized':True}
    response=client.post('/synthetic-education-write-refusal',headers=issue([scope]))
    assert response.status_code==403


def test_normal_token_mint_then_education_authentication(token_client,monkeypatch):
    from modules.organizations import education_access
    monkeypatch.setattr(education_access,'require_education_license',lambda:None)
    client,_=token_client
    @client.app.post('/synthetic-minted-education-write')
    def protected(principal=Depends(education_access.require_education_principal(write=True))):
        return {'authorized':True}
    minted=client.post('/tokens',headers=interactive_headers(),json={'name':'Synthetic EDU test','scopes':['write:education']})
    assert minted.status_code==200
    raw=minted.json().get('token')
    if type(raw) is not str:raise AssertionError('Minted credential shape unavailable')
    response=client.post('/synthetic-minted-education-write',headers={'X-API-Key':raw})
    assert response.status_code==200
    assert response.json()=={'authorized':True}
