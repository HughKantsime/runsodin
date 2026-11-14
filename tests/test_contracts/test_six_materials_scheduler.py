"""The existing scheduler modes read exact enum-backed slot identities."""
import pytest
from tests.test_contracts.test_six_materials import SIX
from tests.test_contracts.test_scheduler_material_routing import (
    fleet, review_db, only_active, load, colors, add_job, need, mode, run, placed)


@pytest.mark.parametrize('material', SIX)
@pytest.mark.parametrize('value,expected', [('off',11),('prefer',12),('require',12)])
def test_six_scheduler_modes(fleet, material, value, expected):
    only_active(fleet, [11,12])
    load(fleet,11,['PLA'])
    load(fleet,12,[material.replace('-','_')])
    colors(fleet,{11:'red',12:'white'})
    add_job(fleet,910,colors_required='red')
    need(fleet,910,material.replace('-','_'))
    mode(fleet,value)
    run(fleet)
    assert placed(fleet,910)==('scheduled',expected)
