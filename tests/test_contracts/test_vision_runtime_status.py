"""Actual daemon-file boundary, stale/mismatched reports cannot claim readiness."""
import json
import os
import pytest
from modules.vision.runtime_status import write_status,read_status,reported_readiness


def publish(tmp_path,now=100):
    directory=tmp_path/'runtime'
    write_status(str(directory),'a'*32,1,{'spaghetti':{'id':7,'state':'ready','fingerprint':'b'*64}},now=now)
    return directory


def test_fresh_exact_model_readiness(tmp_path):
    directory=publish(tmp_path)
    report=read_status(str(directory),now=100)
    assert reported_readiness(report,'spaghetti',7,'b'*64)=={'state':'ready','reported_at':100}


@pytest.mark.parametrize('now',[99,191])
def test_future_or_stale_report_is_unknown(tmp_path,now):
    directory=publish(tmp_path)
    assert read_status(str(directory),now=now) is None


@pytest.mark.parametrize('identity,fingerprint',[(8,'b'*64),(7,'c'*64),(7,'')])
def test_activation_or_file_change_does_not_reuse_fresh_report(tmp_path,identity,fingerprint):
    report=read_status(str(publish(tmp_path)),now=100)
    assert reported_readiness(report,'spaghetti',identity,fingerprint)['state']=='unknown'


def test_restart_report_has_new_incarnation(tmp_path):
    directory=publish(tmp_path)
    write_status(str(directory),'c'*32,1,{},now=101)
    report=read_status(str(directory),now=101)
    assert report['incarnation']=='c'*32 and report['models']=={}
    assert reported_readiness(report,'spaghetti',7,'b'*64)['state']=='unknown'


@pytest.mark.parametrize('payload',[b'{' ,b'x'*32769,b'null',b'[]'])
def test_malformed_report_is_unknown(tmp_path,payload):
    directory=publish(tmp_path);(directory/'status.json').write_bytes(payload)
    assert read_status(str(directory),now=100) is None


def test_status_symlink_is_not_followed(tmp_path):
    directory=publish(tmp_path);(directory/'status.json').unlink()
    target=tmp_path/'target';target.write_text('{}')
    (directory/'status.json').symlink_to(target)
    assert read_status(str(directory),now=100) is None
    assert target.read_text()=='{}'


def test_directory_symlink_is_not_followed(tmp_path):
    real=publish(tmp_path);link=tmp_path/'link';link.symlink_to(real,target_is_directory=True)
    assert read_status(str(link),now=100) is None
    with pytest.raises(OSError):write_status(str(link),'a'*32,2,{},now=101)


def test_world_writable_directory_refused(tmp_path):
    directory=publish(tmp_path);directory.chmod(0o777)
    assert read_status(str(directory),now=100) is None
    with pytest.raises(ValueError):write_status(str(directory),'a'*32,2,{},now=101)


def test_oversized_valid_json_with_padding_is_refused(tmp_path):
    directory=publish(tmp_path)
    file=directory/'status.json'
    file.write_bytes(file.read_bytes()+b' '*32768)
    assert read_status(str(directory),now=100) is None


from tests.test_contracts.test_education_review_workflow import review_db


def test_actual_engine_file_api_readiness_and_registry_change(review_db,tmp_path,monkeypatch):
    import asyncio
    import hashlib
    from types import MappingProxyType
    from modules.vision.inference_engine import VisionInferenceEngine,ModelSnapshot
    from modules.vision import inference_engine
    from modules.vision.routes import vision_models as routes
    from modules.vision.models import VisionModel
    from modules.vision.runtime_status import registry_fingerprint
    file=tmp_path/'owned.onnx';file.write_bytes(b'owned immutable model')
    model=VisionModel(name='Owned',detection_type='spaghetti',filename='owned.onnx',input_size=32,is_active=True)
    review_db.add(model);review_db.commit()
    fingerprint=registry_fingerprint(model.id,model.filename,32,None,hashlib.sha256(file.read_bytes()).hexdigest())
    engine=VisionInferenceEngine();engine._registry_available=True;engine._generation=1
    from types import SimpleNamespace
    from modules.vision.output_contract import OutputContract
    class FailedSession:
        failed=False
        def get_inputs(self):return [SimpleNamespace(name='image')]
        def get_outputs(self):return [SimpleNamespace(name='boxes'),SimpleNamespace(name='confs')]
        def run(self,*args,**kwargs):
            if self.failed:raise RuntimeError('Owned runtime failure')
            import numpy as np
            return [np.empty((1,0,1,4),dtype=np.float32),np.empty((1,0,1),dtype=np.float32)]
    session=FailedSession()
    engine._snapshots=MappingProxyType({'spaghetti':ModelSnapshot(1,'ready',session,32,
        OutputContract('obico_boxes_confs',None,1,(0,)),fingerprint,model.id)})
    monkeypatch.setattr(inference_engine,'VISION_MODELS_DIR',str(tmp_path))
    monkeypatch.setattr(routes,'VISION_MODELS_DIR',str(tmp_path))
    engine.publish_runtime_status()
    def listing():return asyncio.run(routes.list_vision_models(current_user={'role':'admin'},db=review_db))
    assert listing()[0]['runtime_status']['state']=='loaded'
    import numpy as np
    verified=engine.infer_observation('spaghetti',np.zeros((32,32,3),dtype=np.uint8))
    assert verified.available
    engine.publish_runtime_status()
    assert listing()[0]['runtime_status']['state']=='ready'
    session.failed=True
    observation=engine.infer_observation('spaghetti',np.zeros((32,32,3),dtype=np.uint8))
    assert not observation.available and not engine.has_model('spaghetti')
    engine.publish_runtime_status()
    assert listing()[0]['runtime_status']['state']=='unavailable'
    model.input_size=64;review_db.commit()
    assert listing()[0]['runtime_status']['state']=='unknown'
    model.input_size=32;review_db.commit()
    file.write_bytes(b'replaced model')
    assert listing()[0]['runtime_status']['state']=='unknown'
    engine._snapshots=MappingProxyType({});engine._generation=2;engine.publish_runtime_status()
    assert listing()[0]['runtime_status']['state']=='unknown'
