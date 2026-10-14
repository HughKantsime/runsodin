"""Actual inference postprocessing; synthetic outputs do not certify model accuracy."""
import numpy as np
import pytest
from modules.vision.inference_engine import VisionInferenceEngine


def detection(confidence,box):return {'confidence':confidence,'bbox':box,'class_id':0}

def test_nms_keeps_highest_duplicate_and_distinct_failure():
    engine=VisionInferenceEngine()
    low=detection(0.65,[0,0,10,10]);high=detection(0.95,[0,0,10,10]);distinct=detection(0.8,[20,20,30,30])
    assert engine._nms([low,distinct,high],0.45)==[high,distinct]

@pytest.mark.parametrize('items',[[],[detection(0.8,[0,0,10,10])]])
def test_nms_empty_and_single_detection(items):
    assert VisionInferenceEngine()._nms(items,0.45)==items

def test_obico_confidence_filter_coordinate_scaling_and_deduplication():
    boxes=np.array([[[[0,0,0.5,0.5]],[[0,0,0.5,0.5]],[[0.5,0.5,1,1]],[[0.2,0.2,0.3,0.3]]]],dtype=np.float32)
    confidences=np.array([[[0.7],[0.9],[0.8],[0.2]]],dtype=np.float32)
    results=VisionInferenceEngine()._postprocess_obico([boxes,confidences],['boxes','confs'],(100,200,3),640)
    assert len(results)==2
    assert results[0]['confidence']==pytest.approx(0.9)
    assert results[0]['bbox']==pytest.approx([0,0,100,50])
    assert results[1]['bbox']==pytest.approx([100,50,200,100])

from tests.test_contracts.test_vision_decision_behavior import vision
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.mark.parametrize('kind,layer',[('spaghetti',10),('first_layer',1),('detachment',10)])
@pytest.mark.parametrize('threshold,confidence,expected',[(0.85,0.8,False),(0.85,0.85,True),(0.4,0.5,True)])
def test_configured_threshold_controls_each_failure_type(vision,monkeypatch,kind,layer,threshold,confidence,expected):
    thread,model,module=vision
    thread.current_layer=layer
    thread.settings[kind+'_threshold']=threshold
    model.confidence=confidence
    model.has_model=lambda name:name==kind
    detections=[]
    monkeypatch.setattr(thread,'_on_detection',lambda name,det,frame:detections.append(name))
    for _ in range(3):thread._capture_and_analyze()
    assert bool(detections)==expected
    assert all(name==kind for name in detections)

@pytest.mark.parametrize('count',[0,1])
def test_obico_handles_empty_and_single_box_outputs(count):
    boxes=np.array([[[[0.1,0.2,0.5,0.6]]]],dtype=np.float32) if count else np.empty((1,0,1,4),dtype=np.float32)
    confidences=np.array([[[0.9]]],dtype=np.float32) if count else np.empty((1,0,1),dtype=np.float32)
    result=VisionInferenceEngine()._postprocess_obico([boxes,confidences],['boxes','confs'],(100,200,3),640)
    assert len(result)==count
    if count:
        assert result[0]['confidence']==pytest.approx(0.9)
        assert result[0]['bbox']==pytest.approx([20,20,100,60])


def test_actual_inference_entrypoint_preserves_single_obico_detection():
    from types import SimpleNamespace
    engine=VisionInferenceEngine()
    calls=[]
    class Session:
        def get_inputs(self):return [SimpleNamespace(name='image')]
        def get_outputs(self):return [SimpleNamespace(name='confs'),SimpleNamespace(name='boxes')]
        def run(self,names,inputs):
            blob=inputs['image']
            assert blob.shape==(1,3,32,32) and blob.dtype==np.float32
            assert np.all(blob==1)
            calls.append(True)
            return [np.array([[[0.9]]],dtype=np.float32),np.array([[[[0.1,0.2,0.5,0.6]]]],dtype=np.float32)]
    from types import MappingProxyType
    from modules.vision.inference_engine import ModelSnapshot
    from modules.vision.output_contract import OutputContract
    engine._registry_available=True
    engine._snapshots=MappingProxyType({'spaghetti':ModelSnapshot(1,'ready',Session(),32,
        OutputContract('obico_boxes_confs',None,1,(0,)))})
    results=engine.infer('spaghetti',np.full((100,200,3),255,dtype=np.uint8))
    assert calls==[True] and len(results)==1
    assert results[0]['bbox']==pytest.approx([20,20,100,60])
    assert results[0]['confidence']==pytest.approx(0.9)
