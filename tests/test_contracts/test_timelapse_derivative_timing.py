from fractions import Fraction
import pytest
from core.timelapse_recipes import make_recipe
from core.timelapse_derivative_timing import frame_plan, validate_timestamps

@pytest.mark.parametrize('speed,p,q',[(.5,1,2),(1,1,1),(1.5,3,2),(2,2,1),(4,4,1),(8,8,1)])
@pytest.mark.parametrize('count',[1,7,31,16384])
def test_exact_speed_and_reusable_result(speed,p,q,count):
    plan=frame_plan(make_recipe('speed',multiplier=speed),count)
    assert len(plan)==(count*q+p-1)//p
    assert list(plan)==[j*p//q for j in range(len(plan))]
    assert len(frame_plan(make_recipe('speed',multiplier=8),len(plan)))>=1
    duration=Fraction(len(plan),30)
    assert duration>0

def test_upper_bound_preflight_and_chain():
    result=frame_plan(make_recipe('speed',multiplier=.5),16384)
    assert len(result)==32768
    assert len(frame_plan(make_recipe('trim',start_seconds=0,end_seconds=1),len(result)))==30
    with pytest.raises(ValueError,match='reusable'):
        frame_plan(make_recipe('speed',multiplier=.5),32768)

def test_end_exclusive_and_empty_grid():
    assert list(frame_plan(make_recipe('trim',start_seconds=.04,end_seconds=.1),30))==[2]
    with pytest.raises(ValueError,match='no source frame'):
        frame_plan(make_recipe('trim',start_seconds=.001,end_seconds=.002),30)
    with pytest.raises(ValueError,match='exceeds'):
        frame_plan(make_recipe('trim',start_seconds=0,end_seconds=2),30)

@pytest.mark.parametrize('stamps,base', [([0,512,1024],'1/15360'),([0],'1/30')])
def test_exact_pts(stamps,base):
    assert validate_timestamps(stamps,base)==len(stamps)

@pytest.mark.parametrize('stamps',[[512],[0,513],[0,512,512],[]])
def test_reject_shift_vfr_or_empty(stamps):
    with pytest.raises(ValueError):validate_timestamps(stamps,'1/15360')
