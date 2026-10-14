"""Global model authority through real hashed scoped-token authentication."""
import json
import secrets
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def model_client(review_db, monkeypatch):
    from core.auth import hash_password
    from core.db import get_db
    from modules.vision.routes import vision_models
    monkeypatch.delenv('API_KEY', raising=False)
    app=FastAPI()
    app.include_router(vision_models.router)
    def database():
        with Session(review_db.get_bind()) as db:yield db
    app.dependency_overrides[get_db]=database
    def issue(scopes, tenant):
        review_db.execute(text('UPDATE users SET group_id=:org WHERE id=3'),{'org':1 if tenant else None})
        raw='odin_'+secrets.token_urlsafe(32)
        review_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'Owned vision token',:hash,:prefix,:scopes)"),
            {'hash':hash_password(raw),'prefix':raw[:10],'scopes':json.dumps(scopes)})
        review_db.commit()
        return {'X-API-Key':raw}
    return TestClient(app),issue


@pytest.mark.parametrize('scopes,tenant',[(['read'],False),([],False),(['write'],True)])
@pytest.mark.parametrize('operation',['upload','activate','delete'])
def test_unauthorized_token_cannot_mutate_global_registry(model_client,review_db,scopes,tenant,operation):
    client,issue=model_client
    headers=issue(scopes,tenant)
    before=review_db.execute(text('SELECT COUNT(*) FROM vision_models')).scalar_one()
    if operation=='upload':
        response=client.post('/vision/models?name=Owned&detection_type=spaghetti',
            headers=headers,files={'file':('owned.onnx',b'no filesystem authority')})
    elif operation=='activate':response=client.patch('/vision/models/999999/activate',headers=headers)
    else:response=client.delete('/vision/models/999999',headers=headers)
    # Empty stored scopes are rejected by the real credential parser.
    assert response.status_code==(401 if not scopes else 403)
    assert review_db.execute(text('SELECT COUNT(*) FROM vision_models')).scalar_one()==before


@pytest.mark.parametrize('operation',['activate','delete'])
def test_global_write_token_reaches_registry_lookup(model_client,operation):
    client,issue=model_client
    headers=issue(['write'],False)
    response=(client.patch('/vision/models/999999/activate',headers=headers)
              if operation=='activate' else client.delete('/vision/models/999999',headers=headers))
    assert response.status_code==404


@pytest.mark.parametrize('tenant,expected',[(False,200),(True,403)])
def test_global_registry_list_requires_installation_read_authority(model_client,tenant,expected):
    client,issue=model_client
    assert client.get('/vision/models',headers=issue(['read'],tenant)).status_code==expected


def test_global_write_activation_switches_only_its_detection_type(model_client,review_db):
    from modules.vision.models import VisionModel
    previous=VisionModel(name='Previous',detection_type='spaghetti',filename='old.onnx',is_active=True)
    target=VisionModel(name='Target',detection_type='spaghetti',filename='target.onnx',is_active=False)
    other=VisionModel(name='Other type',detection_type='detachment',filename='other.onnx',is_active=True)
    review_db.add_all([previous,target,other]);review_db.commit()
    client,issue=model_client
    response=client.patch(f'/vision/models/{target.id}/activate',headers=issue(['write'],False))
    assert response.status_code==200
    review_db.expire_all()
    assert not previous.is_active and target.is_active and other.is_active


def test_global_write_cannot_retire_active_model(model_client,review_db):
    from modules.vision.models import VisionModel
    model=VisionModel(name='Active',detection_type='spaghetti',filename='owned.onnx',is_active=True)
    review_db.add(model);review_db.commit();identity=model.id
    client,issue=model_client
    response=client.delete(f'/vision/models/{identity}',headers=issue(['write'],False))
    assert response.status_code==400
    review_db.expire_all()
    assert review_db.query(VisionModel).filter(VisionModel.id==identity).one().is_active
