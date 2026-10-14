"""Exact saved trim settings must survive recovery without caller arguments."""
import pytest


@pytest.mark.parametrize('value,expected',[(0,'0'),(-0.0,'0'),(1e-6,'0.000001'),(1.25,'1.25'),(5e-324,'0.'+'0'*323+'5')])
def test_trim_timestamp_is_plain_and_preserves_supplied_precision(value,expected):
    from core.timelapse_recipes import canonical_seconds
    assert canonical_seconds(value)==expected


@pytest.mark.parametrize('value',[True,'0.1',float('nan'),float('inf'),-1,9223372036855])
def test_invalid_or_unsupported_timestamp_refuses(value):
    from core.timelapse_recipes import canonical_seconds
    with pytest.raises(ValueError):canonical_seconds(value)


def test_huge_integer_refuses_before_string_conversion():
    from core.timelapse_recipes import canonical_seconds
    with pytest.raises(ValueError,match='Timestamp outside supported range'):
        canonical_seconds(10**10000)


@pytest.mark.parametrize('value',['1e-6','01','1.0','-0','0.0','NaN','Infinity','9223372036854.775808','0.'+'0'*340+'1'])
def test_saved_time_refuses_noncanonical_or_unsupported_text(value):
    from core.timelapse_recipes import stored_seconds
    with pytest.raises(ValueError):stored_seconds(value)


def test_saved_time_preserves_exact_supported_boundary():
    from decimal import Decimal
    from core.timelapse_recipes import stored_seconds,canonical_seconds
    assert stored_seconds('9223372036854.775807')==Decimal('9223372036854.775807')
    assert canonical_seconds(1000)=='1000'
    assert stored_seconds(canonical_seconds(5e-324))==Decimal('5e-324')


@pytest.mark.parametrize('multiplier,pair',[(0.5,(1,2)),(1,(1,1)),(1.5,(3,2)),(2,(2,1)),(4,(4,1)),(8,(8,1))])
def test_speed_recipe_records_exact_supported_ratio(multiplier,pair):
    from core.timelapse_recipes import make_recipe
    recipe=make_recipe('speed',multiplier=multiplier)
    assert (recipe.speed_numerator,recipe.speed_denominator)==pair
    assert recipe.recipe_version==1 and recipe.encode_fps is None


def test_trim_recipe_uses_exact_ordered_saved_times():
    from core.timelapse_recipes import make_recipe,recipe_from_row
    recipe=make_recipe('trim',start_seconds=1e-6,end_seconds=0.123456789)
    assert (recipe.trim_start,recipe.trim_end)==('0.000001','0.123456789')
    row={'kind':'trim',**recipe.columns()}
    restored=recipe_from_row(row)
    assert restored==recipe
    with pytest.raises(ValueError):make_recipe('trim',start_seconds=1,end_seconds=1)


def test_frame_and_encode_recipe_have_no_edit_parameters():
    from core.timelapse_recipes import make_recipe
    assert make_recipe('frame').encode_fps is None
    assert make_recipe('encode').encode_fps==30
    with pytest.raises(ValueError):make_recipe('encode',multiplier=2)


@pytest.mark.parametrize('kind,field,value',[
    ('frame','recipe_version',True),('frame','recipe_version',1.0),('frame','recipe_version',2),
    ('frame','encode_fps',30),('encode','encode_fps',30.0),('encode','encode_fps',None),
    ('speed','speed_numerator',True),('speed','speed_denominator',0),
    ('speed','speed_numerator',6),('trim','trim_start','1e-6'),
    ('trim','trim_start','2'),('trim','trim_end',None),
])
def test_saved_recipe_corruption_refuses(kind,field,value):
    from core.timelapse_recipes import make_recipe,recipe_from_row
    args={'trim':{'start_seconds':0,'end_seconds':1},'speed':{'multiplier':2}}.get(kind,{})
    row={'kind':kind,**make_recipe(kind,**args).columns()}
    row[field]=value
    with pytest.raises(ValueError):recipe_from_row(row)


def test_saved_recipe_missing_field_refuses_and_recipe_is_immutable():
    from dataclasses import FrozenInstanceError
    from core.timelapse_recipes import make_recipe,recipe_from_row
    recipe=make_recipe('speed',multiplier=1.5)
    row={'kind':'speed',**recipe.columns()};del row['trim_end']
    with pytest.raises(ValueError,match='Saved recipe fields missing'):recipe_from_row(row)
    with pytest.raises(FrozenInstanceError):recipe.speed_numerator=4


@pytest.mark.parametrize('value',[True,0,3,float('inf'),float('nan')])
def test_unsupported_speed_request_refuses(value):
    from core.timelapse_recipes import make_recipe
    with pytest.raises(ValueError):make_recipe('speed',multiplier=value)
