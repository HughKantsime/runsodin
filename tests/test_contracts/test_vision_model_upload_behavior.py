"""Owned-file probe of actual model upload, no production filesystem access."""
import asyncio
import io
import os
from types import SimpleNamespace
import pytest
from fastapi import UploadFile
from tests.test_contracts.test_education_review_workflow import review_db


def test_same_client_filename_does_not_replace_registered_model(review_db, tmp_path, monkeypatch):
    import secrets
    monkeypatch.setenv("JWT_SECRET_KEY", secrets.token_urlsafe(48))
    from modules.vision.routes import vision_models as routes
    from modules.vision.models import VisionModel
    monkeypatch.setattr(routes, 'os', SimpleNamespace(**(vars(os) | {
        'makedirs': lambda *args, **kwargs: None,
        'path': SimpleNamespace(join=lambda directory, name: str(tmp_path / name)),
    })))
    def upload(payload):
        return asyncio.run(routes.upload_vision_model(
            file=UploadFile(filename='shared.onnx',file=io.BytesIO(payload)),
            name='Owned probe',detection_type='spaghetti',input_size=640,
            version='probe',inference_contract=None,current_user={'id':1,'role':'admin'},db=review_db,
        ))
    first=upload(b'first-model-evidence')
    first_row=review_db.query(VisionModel).filter(VisionModel.id==first['id']).one()
    first_path=tmp_path / first_row.filename
    assert first_path.read_bytes()==b'first-model-evidence'
    upload(b'second-model-evidence')
    assert first_path.read_bytes()==b'first-model-evidence', 'Second upload replaced the earlier registered model bytes'


def test_uncertain_registration_retains_newly_owned_model(tmp_path, monkeypatch):
    import secrets
    monkeypatch.setenv('JWT_SECRET_KEY',secrets.token_urlsafe(48))
    from modules.vision.routes import vision_models as routes
    monkeypatch.setattr(routes,'os',SimpleNamespace(**(vars(os) | {
        'makedirs':lambda *a,**k:None,
        'path':SimpleNamespace(join=lambda directory,name:str(tmp_path/name))})))
    class UncertainDB:
        def add(self, model):pass
        def commit(self):raise OSError('Synthetic lost acknowledgement')
        def rollback(self):pass
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as failure:
        asyncio.run(routes.upload_vision_model(file=UploadFile(filename='owned.onnx',
            file=io.BytesIO(b'owned immutable model')), name='Owned',detection_type='spaghetti',
            version=None,input_size=640,inference_contract=None,
            current_user={'id':1,'role':'admin'},db=UncertainDB()))
    assert failure.value.status_code==503
    files=list(tmp_path.iterdir())
    assert len(files)==1 and files[0].read_bytes()==b'owned immutable model'


def test_failed_logical_retirement_preserves_registry_and_model_bytes(review_db,tmp_path,monkeypatch):
    from modules.vision.routes import vision_models as routes
    from modules.vision.models import VisionModel
    owned=tmp_path/'legacy.onnx'
    owned.write_bytes(b'preserved model bytes')
    model=VisionModel(name='Owned',detection_type='spaghetti',filename=str(owned),is_active=False)
    review_db.add(model);review_db.commit();identity=model.id
    def uncertain():raise OSError('Synthetic commit acknowledgement failure')
    monkeypatch.setattr(review_db,'commit',uncertain)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as failure:
        asyncio.run(routes.delete_vision_model(identity,current_user={'role':'admin'},db=review_db))
    assert failure.value.status_code==503
    assert owned.read_bytes()==b'preserved model bytes'
    assert review_db.query(VisionModel).filter(VisionModel.id==identity).one()


def test_successful_logical_retirement_keeps_immutable_bytes(review_db,tmp_path):
    from modules.vision.routes import vision_models as routes
    from modules.vision.models import VisionModel
    owned=tmp_path/'legacy.onnx';owned.write_bytes(b'live session may still own bytes')
    model=VisionModel(name='Owned',detection_type='spaghetti',filename=str(owned),is_active=False)
    review_db.add(model);review_db.commit();identity=model.id
    result=asyncio.run(routes.delete_vision_model(identity,current_user={'role':'admin'},db=review_db))
    assert result=={'id':identity,'deleted':True}
    assert review_db.query(VisionModel).filter(VisionModel.id==identity).first() is None
    assert owned.read_bytes()==b'live session may still own bytes'
