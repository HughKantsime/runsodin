"""Inference snapshot and actionable-class acceptance, no physical commands."""
from types import SimpleNamespace, MappingProxyType
import numpy as np
import pytest
from modules.vision.inference_engine import VisionInferenceEngine, ModelSnapshot
from modules.vision.output_contract import OutputContract


def ready_engine(layout, rows, callback=None):
    engine=VisionInferenceEngine()
    contract=OutputContract('yolov8_class_scores',layout,2,(0,))
    class Session:
        def get_inputs(self):return [SimpleNamespace(name='image')]
        def get_outputs(self):return [SimpleNamespace(name='output')]
        def run(self, names, inputs):
            if callback:callback(engine)
            return [rows.T[None] if layout=='channels_first' else rows[None]]
    engine._registry_available=True;engine._generation=1
    engine._snapshots=MappingProxyType({'spaghetti':ModelSnapshot(1,'ready',Session(),32,contract,'owned')})
    return engine


@pytest.mark.parametrize('layout',['channels_first','rows_first'])
@pytest.mark.parametrize('count',[0,1,2,4,6,10])
def test_actual_infer_uses_explicit_layout_for_all_counts(layout,count):
    rows=np.tile([10.,10.,2.,2.,.8,.1],(count,1))
    engine=ready_engine(layout,rows)
    observation=engine.infer_observation('spaghetti',np.zeros((32,32,3),dtype=np.uint8))
    assert observation.available
    assert len(observation.detections)==(1 if count else 0)
    assert engine.observation_current('spaghetti',observation)


def test_excluded_class_does_not_suppress_actionable_class():
    engine=ready_engine('rows_first',np.array([[10.,10.,2.,2.,.1,.99],[10.,10.,2.,2.,.8,.1]]))
    observation=engine.infer_observation('spaghetti',np.zeros((32,32,3),dtype=np.uint8))
    assert observation.available and len(observation.detections)==1
    assert observation.detections[0]['class_id']==0
    assert observation.detections[0]['confidence']==pytest.approx(.8)


def test_reload_during_inference_invalidates_old_ready_observation():
    def reload(engine):
        with engine._lock:
            engine._generation=2
            engine._snapshots=MappingProxyType({'spaghetti':ModelSnapshot(2,'unavailable')})
    engine=ready_engine('rows_first',np.array([[10.,10.,2.,2.,.8,.1]]),reload)
    observation=engine.infer_observation('spaghetti',np.zeros((32,32,3),dtype=np.uint8))
    assert not observation.available
    assert not engine.observation_current('spaghetti',observation)


def test_runtime_session_failure_is_unavailable_not_successful_empty():
    def fail(engine):raise RuntimeError('Owned session failure')
    engine=ready_engine('rows_first',np.empty((0,6)),fail)
    observation=engine.infer_observation('spaghetti',np.zeros((32,32,3),dtype=np.uint8))
    assert not observation.available


def test_unread_registry_does_not_authorize_plate_heuristic():
    engine=VisionInferenceEngine()
    generation,state=engine.configuration_state('build_plate_empty')
    assert state=='unavailable'
    assert not engine.heuristic_current('build_plate_empty',generation)

from tests.test_contracts.test_education_review_workflow import review_db

@pytest.mark.parametrize('failure',['generation_change','unavailable'])
def test_confirmation_history_cannot_cross_snapshot_generation(monkeypatch,failure):
    from modules.vision.detection_thread import PrinterVisionThread
    engine=ready_engine('rows_first',np.array([[10.,10.,2.,2.,.8,.1]]))
    thread=PrinterVisionThread(9,'Owned',engine,{'spaghetti_enabled':1},10,23,'RUNNING')
    thread._history['spaghetti']=[True]
    thread._history_generations={'spaghetti':1}
    if failure=='generation_change':
        old=engine._snapshots['spaghetti']
        engine._generation=2
        engine._snapshots=MappingProxyType({'spaghetti':ModelSnapshot(2,'ready',old.session,32,old.contract,'new')})
    else:
        engine._generation=2
        engine._snapshots=MappingProxyType({'spaghetti':ModelSnapshot(2,'unavailable')})
    monkeypatch.setattr(thread,'_capture_frame',lambda:np.zeros((32,32,3),dtype=np.uint8))
    actions=[];monkeypatch.setattr(thread,'_on_detection',lambda *args:actions.append(args))
    thread._capture_and_analyze()
    assert actions==[]
    assert thread._history['spaghetti']==([True] if failure=='generation_change' else [])


def test_reload_after_first_current_check_cannot_admit_decision(monkeypatch):
    from modules.vision.detection_thread import PrinterVisionThread
    engine=ready_engine('rows_first',np.array([[10.,10.,2.,2.,.8,.1]]))
    thread=PrinterVisionThread(9,'Owned',engine,{'spaghetti_enabled':1},10,23,'RUNNING')
    thread._history['spaghetti']=[True]
    thread._history_generations={'spaghetti':1}
    original=engine.observation_current;calls=[]
    def race(kind,observation):
        valid=original(kind,observation);calls.append(True)
        # First call is infer's completion fence; second is thread's early check.
        if len(calls)==2:
            engine._generation=2
            engine._snapshots=MappingProxyType({'spaghetti':ModelSnapshot(2,'unavailable')})
        return valid
    monkeypatch.setattr(engine,'observation_current',race)
    monkeypatch.setattr(thread,'_capture_frame',lambda:np.zeros((32,32,3),dtype=np.uint8))
    actions=[];monkeypatch.setattr(thread,'_on_detection',lambda *args:actions.append(args))
    thread._capture_and_analyze()
    assert actions==[] and thread._history['spaghetti']==[]


def test_session_constructor_receives_exact_verified_bytes(review_db,tmp_path,monkeypatch):
    from sqlalchemy import text
    from modules.vision import inference_engine as module
    path=tmp_path/'owned.onnx';path.write_bytes(b'original verified bytes')
    review_db.execute(text("INSERT INTO vision_models(name,detection_type,filename,input_size,is_active) VALUES('Owned','spaghetti','owned.onnx',32,1)"));review_db.commit()
    received=[]
    class Session:
        def __init__(self,source,*args,**kwargs):
            path.write_bytes(b'replacement during constructor')
            received.append(source)
        def get_inputs(self):return [SimpleNamespace(name='image',type='tensor(float)',shape=[1,3,32,32])]
        def get_outputs(self):return [SimpleNamespace(name='boxes',shape=[1,'N',1,4]),SimpleNamespace(name='confs',shape=[1,'N',1])]
    runtime=SimpleNamespace(SessionOptions=lambda:SimpleNamespace(),
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL=1),InferenceSession=Session)
    monkeypatch.setattr(module,'ort',runtime);monkeypatch.setattr(module,'engine',review_db.get_bind())
    monkeypatch.setattr(module,'VISION_MODELS_DIR',str(tmp_path))
    engine=VisionInferenceEngine();engine.reload_models()
    assert engine.has_model('spaghetti')
    assert received==[b'original verified bytes']
    assert path.read_bytes()!=received[0]


def test_reload_after_history_sweep_cannot_reuse_previous_positive(monkeypatch):
    from modules.vision.detection_thread import PrinterVisionThread
    engine=ready_engine('rows_first',np.array([[10.,10.,2.,2.,.8,.1]]))
    thread=PrinterVisionThread(9,'Owned',engine,{'spaghetti_enabled':1},10,23,'RUNNING')
    thread._history['spaghetti']=[True];thread._history_generations={'spaghetti':1}
    original=engine.infer_observation
    def reload_before_infer(kind,frame):
        old=engine._snapshots[kind]
        engine._generation=2
        engine._snapshots=MappingProxyType({kind:ModelSnapshot(2,'ready',old.session,32,old.contract,'new')})
        return original(kind,frame)
    monkeypatch.setattr(engine,'infer_observation',reload_before_infer)
    monkeypatch.setattr(thread,'_capture_frame',lambda:np.zeros((32,32,3),dtype=np.uint8))
    actions=[];monkeypatch.setattr(thread,'_on_detection',lambda *args:actions.append(args))
    thread._capture_and_analyze()
    assert actions==[] and thread._history['spaghetti']==[True]
    assert thread._history_generations['spaghetti']==2


def test_same_model_id_changed_contract_reloads_bound_session(review_db,tmp_path,monkeypatch):
    import json
    from sqlalchemy import text
    from modules.vision import inference_engine as module
    path=tmp_path/'owned.onnx';path.write_bytes(b'owned model bytes')
    def metadata(positive):return json.dumps({'inference_contract':{'version':1,
        'format':'yolov8_class_scores','layout':'rows_first','class_count':2,'positive_class_ids':[positive]}})
    review_db.execute(text("INSERT INTO vision_models(name,detection_type,filename,input_size,is_active,metadata_json) VALUES('Owned','spaghetti','owned.onnx',32,1,:metadata)"),{'metadata':metadata(0)});review_db.commit()
    sessions=[]
    class Session:
        def __init__(self,source,*args,**kwargs):sessions.append(self)
        def get_inputs(self):return [SimpleNamespace(name='image',type='tensor(float)',shape=[1,3,32,32])]
        def get_outputs(self):return [SimpleNamespace(name='output',shape=[1,10,6])]
    runtime=SimpleNamespace(SessionOptions=lambda:SimpleNamespace(),
        GraphOptimizationLevel=SimpleNamespace(ORT_ENABLE_ALL=1),InferenceSession=Session)
    monkeypatch.setattr(module,'ort',runtime);monkeypatch.setattr(module,'engine',review_db.get_bind())
    monkeypatch.setattr(module,'VISION_MODELS_DIR',str(tmp_path))
    engine=VisionInferenceEngine();engine.reload_models();old=engine._snapshots['spaghetti']
    engine.reload_models();unchanged=engine._snapshots['spaghetti']
    assert unchanged.session is old.session and len(sessions)==1
    review_db.execute(text("UPDATE vision_models SET metadata_json=:metadata WHERE detection_type='spaghetti'"),{'metadata':metadata(1)});review_db.commit()
    engine.reload_models();new=engine._snapshots['spaghetti']
    assert len(sessions)==2 and new.session is not old.session
    assert new.contract.positive_class_ids==(1,) and old.contract.positive_class_ids==(0,)
    assert new.fingerprint!=old.fingerprint and new.model_id==old.model_id
