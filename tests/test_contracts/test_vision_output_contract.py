"""Explicit layouts accept all box counts and reject untrustworthy tensors."""
import numpy as np
import pytest
from modules.vision.output_contract import OutputContract, InvalidModelOutput


def contract(layout='rows_first', count=2):
    return OutputContract.from_metadata({'inference_contract':{
        'version':1,'format':'yolov8_class_scores','layout':layout,
        'class_count':count,'positive_class_ids':[0],
    }})


@pytest.mark.parametrize('layout',['rows_first','channels_first'])
@pytest.mark.parametrize('n',[0,1,2,4,6,10])
def test_explicit_layout_all_box_counts_including_square(layout,n):
    rows=np.zeros((n,6),dtype=np.float32)
    rows[:,:4]=[10,10,2,2]
    rows[:,4:]=[.8,.2]
    output=rows.T[None] if layout=='channels_first' else rows[None]
    np.testing.assert_array_equal(contract(layout).yolo_rows([output]),rows)


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-.1,1.1])
def test_invalid_scores_refused(value):
    rows=np.array([[[10,10,2,2,value,.1]]])
    with pytest.raises(InvalidModelOutput):contract().yolo_rows([rows])


@pytest.mark.parametrize('shape',[(2,1,6),(1,6),(1,1,5),(1,1,7)])
def test_wrong_batch_rank_feature_width_refused(shape):
    with pytest.raises(InvalidModelOutput):contract().yolo_rows([np.zeros(shape)])


@pytest.mark.parametrize('field,value',[('layout',None),('class_count',True),
    ('class_count',0),('positive_class_ids',[]),('positive_class_ids',[2]),
    ('positive_class_ids',[0,0]),('format','guessed'),('version',2),('version',True)])
def test_invalid_contract_refused(field,value):
    metadata={'version':1,'format':'yolov8_class_scores','layout':'rows_first',
              'class_count':2,'positive_class_ids':[0]}
    metadata[field]=value
    with pytest.raises(InvalidModelOutput):OutputContract.from_metadata({'inference_contract':metadata})


@pytest.mark.parametrize('n',[0,1,4])
def test_obico_shape_validation_keeps_detection_axis(n):
    policy=OutputContract('obico_boxes_confs',None,1,(0,))
    boxes=np.tile([.1,.2,.5,.6],(n,1)).reshape(1,n,1,4)
    scores=np.full((1,n,1),.9)
    b,c=policy.obico_arrays([scores,boxes],['confs','boxes'])
    assert b.shape==(n,4) and c.shape==(n,)


def test_obico_mismatched_detection_axes_refused():
    policy=OutputContract('obico_boxes_confs',None,1,(0,))
    with pytest.raises(InvalidModelOutput):
        policy.obico_arrays([np.zeros((1,2,1,4)),np.zeros((1,1,1))],['boxes','confs'])


@pytest.mark.parametrize('field,value',[(0,float('nan')),(1,float('inf')),(2,-1),(3,-1)])
def test_invalid_yolo_coordinates_refused(field,value):
    rows=np.array([[[10.,10.,2.,2.,.8,.1]]])
    rows[0,0,field]=value
    with pytest.raises(InvalidModelOutput):contract().yolo_rows([rows])


@pytest.mark.parametrize('boxes,scores',[
    ([.5,.2,.1,.6],[.9]),([float('nan'),.2,.5,.6],[.9]),
    ([.1,.2,float('inf'),.6],[.9]),([.1,.2,.5,.6],[float('nan')]),
    ([.1,.2,.5,.6],[1.1]),
])
def test_invalid_obico_values_refused(boxes,scores):
    policy=OutputContract('obico_boxes_confs',None,1,(0,))
    with pytest.raises(InvalidModelOutput):
        policy.obico_arrays([np.array(boxes).reshape(1,1,1,4),
                            np.array(scores).reshape(1,1,1)],['boxes','confs'])


@pytest.mark.parametrize('dtype',[bool,complex,object,str])
@pytest.mark.parametrize('count',[0,1])
def test_unsupported_dtypes_refused_even_when_empty(dtype,count):
    with pytest.raises(InvalidModelOutput):
        contract().yolo_rows([np.zeros((1,count,6)).astype(dtype)])
    policy=OutputContract('obico_boxes_confs',None,1,(0,))
    with pytest.raises(InvalidModelOutput):
        policy.obico_arrays([np.zeros((1,count,1,4)).astype(dtype),
            np.zeros((1,count,1)).astype(dtype)],['boxes','confs'])


def test_ordered_out_of_image_obico_corners_preserve_scale_without_clamp():
    policy=OutputContract('obico_boxes_confs',None,1,(0,))
    original=np.array([[[[-2.,-.5,3.7,1.2]]]],dtype=np.float32)
    boxes,scores=policy.obico_arrays([original,np.array([[[.9]]])],['boxes','confs'])
    np.testing.assert_array_equal(boxes,original.reshape(1,4))


@pytest.mark.parametrize('boundary',[-np.finfo(np.float32).eps,1+np.finfo(np.float32).eps])
def test_float32_confidence_boundary_roundoff_clamped(boundary):
    policy=OutputContract('obico_boxes_confs',None,1,(0,))
    boxes=np.array([[[[0.,0.,1.,1.]]]],dtype=np.float32)
    _,scores=policy.obico_arrays([boxes,np.array([[[boundary]]],dtype=np.float32)],['boxes','confs'])
    assert scores[0]==(0 if boundary<0 else 1)
    rows=np.array([[[1.,1.,1.,1.,boundary,.1]]],dtype=np.float32)
    assert contract().yolo_rows([rows])[0,4]==scores[0]


@pytest.mark.parametrize('boundary',[
    np.nextafter(np.float32(-np.finfo(np.float32).eps),np.float32(-np.inf)),
    np.nextafter(np.float32(1+np.finfo(np.float32).eps),np.float32(np.inf)),
])
def test_float32_values_immediately_beyond_tolerance_refused(boundary):
    with pytest.raises(InvalidModelOutput):
        contract().yolo_rows([np.array([[[1.,1.,1.,1.,boundary,.1]]],dtype=np.float32)])


def test_float64_does_not_inherit_float32_roundoff_allowance():
    with pytest.raises(InvalidModelOutput):
        contract().yolo_rows([np.array([[[1.,1.,1.,1.,-np.finfo(np.float32).eps,.1]]],dtype=np.float64)])
