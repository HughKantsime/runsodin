"""Vigil runs real ONNX inference for every detection type given an uploaded model (F03-3.1-02).

Acceptance test with the genuine bundled model bytes. Set ODIN_REAL_VISION_MODEL to a
hydrated obico_spaghetti.onnx (Git LFS oid 0a6ebd8e...); skipped otherwise.
"""
import asyncio,hashlib,io,os
from pathlib import Path
import numpy as np
import pytest
from fastapi import UploadFile
from tests.test_contracts.test_education_review_workflow import review_db

MODEL=os.environ.get('ODIN_REAL_VISION_MODEL')
LFS_OID='0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa'
# live_environment: needs a built image, Docker, network or an external checkout; the contract
# gate deselects it (tests/test_contracts/live_environment_inventory.txt) and the certification
# batches run it with the environment set.
pytestmark = [pytest.mark.live_environment, pytest.mark.skipif(not MODEL or not Path(MODEL).is_file(),reason='ODIN_REAL_VISION_MODEL not provided')]


@pytest.mark.parametrize('kind',['first_layer','detachment','build_plate_empty','spaghetti'])
def test_uploaded_model_runs_real_inference_for_detection_type(review_db,tmp_path,monkeypatch,kind):
    payload=Path(MODEL).read_bytes()
    assert hashlib.sha256(payload).hexdigest()==LFS_OID
    from modules.vision.routes import vision_models as routes
    from modules.vision import inference_engine
    monkeypatch.setattr(routes,'VISION_MODELS_DIR',str(tmp_path))
    monkeypatch.setattr(inference_engine,'VISION_MODELS_DIR',str(tmp_path))
    monkeypatch.setattr(inference_engine,'engine',review_db.get_bind())
    admin={'id':1,'role':'admin','group_id':None}
    uploaded=asyncio.run(routes.upload_vision_model(file=UploadFile(filename='owned.onnx',file=io.BytesIO(payload)),
        name=f'Owned {kind}',detection_type=kind,version='acceptance',input_size=416,inference_contract=None,
        current_user=admin,db=review_db))
    asyncio.run(routes.activate_vision_model(model_id=uploaded['id'],current_user=admin,db=review_db))
    engine=inference_engine.VisionInferenceEngine();engine.reload_models()
    assert engine.model_state(kind)=='ready',engine.model_state(kind)
    assert all(engine.model_state(other)!='ready' for other in ('spaghetti','first_layer','detachment','build_plate_empty') if other!=kind)
    observation=engine.infer_observation(kind,np.full((480,640,3),128,dtype=np.uint8))
    assert observation.available is True
    assert isinstance(observation.detections,tuple)
    assert all(0<=d['confidence']<=1 and len(d['bbox'])==4 for d in observation.detections)
