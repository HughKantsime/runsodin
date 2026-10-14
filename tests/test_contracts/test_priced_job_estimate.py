"""Independent pure arithmetic examples; no source/job authority fixtures."""
import pytest
from fractions import Fraction
from dataclasses import FrozenInstanceError
from core.priced_job_estimate import calculate_priced_job_estimate as calculate, EstimateInputError, FIELDS, _check


def source(currency='USD', rate='0', **assumptions):
    values={k:'0' for k in FIELDS}
    values.update(spool_weight='1000',printer_lifespan='1000',parts_per_month='1')
    values.update(assumptions)
    return dict(currency=currency,assumptions=values,material_rates={'PLA':dict(currency=currency,cost_per_gram=rate)})


def cost(bundle,grams='0',hours='0',runs=1):
    return calculate(bundle,material_key='PLA',grams_per_run=grams,hours_per_run=hours,runs=runs)


def test_full_formula_then_runs_once():
    bundle=source(rate='0.02',post_processing_min='10',packing_min='5',support_min='15',hourly_rate='20',printer_wattage='200',electricity_rate='0.3',printer_cost='1000',printer_lifespan='1000',packaging_cost='2',other_costs='1',failure_rate='10',monthly_rent='100',parts_per_month='20')
    # material2 +labor10 +electricity.12 +depreciation2 +packaging2 +other1
    # base17.12, failure1.712, overhead5 ->23.832 perrun; three=71.496
    assert cost(bundle,'100','2',3).amount=='71.50'


def test_zero_and_immutable_result():
    result=cost(source(),runs=10000)
    assert (result.classification,result.currency,result.amount,result.reason)==('job_estimate','USD','0.00',None)
    with pytest.raises(FrozenInstanceError):result.amount='1'

@pytest.mark.parametrize('rate,expected',[('0.005','0.00'),('0.015','0.02'),('0.025','0.02'),('0.035','0.04')])
def test_half_even_once(rate,expected):
    assert cost(source(rate=rate),'1').amount==expected
    assert cost(source(rate='0.005'),'1',runs=3).amount=='0.02'

@pytest.mark.parametrize('rate,expected',[('0.5','0'),('1.5','2'),('2.5','2'),('3.5','4')])
def test_jpy_rounding(rate,expected):assert cost(source('JPY',rate),'1').amount==expected

@pytest.mark.parametrize('currency',['USD','EUR','GBP','CAD','AUD','CHF','NZD'])
def test_supported_currency(currency):assert cost(source(currency,'1'),'1').amount=='1.00'

@pytest.mark.parametrize('value',[0,False,1.5,None,'',' 1','1 ','+1','-1','1e2','01','1.0','0.00','.1','1.1234567891','1000000000000000000','NaN','Infinity'])
def test_invalid_decimal(value):
    with pytest.raises(EstimateInputError):cost(source(rate=value))

@pytest.mark.parametrize('runs',[None,False,True,0,-1,10001,1.0,'1'])
def test_invalid_explicit_run_count(runs):
    with pytest.raises(EstimateInputError):cost(source(),runs=runs)

@pytest.mark.parametrize('field',['spool_weight','printer_lifespan','parts_per_month'])
def test_zero_divisor_refused(field):
    with pytest.raises(EstimateInputError):cost(source(**{field:'0'}))


def test_complete_and_closed_shapes_mixed_currency():
    bundle=source();del bundle['assumptions']['packing_min']
    with pytest.raises(EstimateInputError):cost(bundle)
    bundle=source();bundle['extra']='x'
    with pytest.raises(EstimateInputError):cost(bundle)
    bundle=source();bundle['material_rates']['PLA']['currency']='EUR'
    with pytest.raises(EstimateInputError):cost(bundle)
    bundle=source();bundle['material_rates']['PLA']['extra']='x'
    with pytest.raises(EstimateInputError):cost(bundle)


def test_deterministic_key_and_unknown_no_money():
    bundle=source(rate='2');bundle['material_rates']={'ABS':dict(currency='USD',cost_per_gram='7'),**bundle['material_rates']}
    assert cost(bundle,'1').amount=='2.00'
    for result in (calculate(None),calculate(bundle,material_key='missing'),calculate(bundle,material_key='PLA'),calculate(bundle,material_key='PLA',grams_per_run='1',hours_per_run='1')):
        assert result.classification=='unknown' and result.amount is None and result.currency is None


def test_bounds_and_exact_total_limit():
    assert cost(source(rate='0.000000001'),'1000000000','1000000000',10000).amount=='10000.00'
    with pytest.raises(EstimateInputError):cost(source(),'1000000000.000000001')
    with pytest.raises(EstimateInputError):cost(source(),hours='1000000001')
    assert cost(source(failure_rate='10000')).amount=='0.00'
    with pytest.raises(EstimateInputError):cost(source(failure_rate='10000.000000001'))
    assert cost(source(rate='999999999999999999'),'1').amount=='999999999999999999.00'
    with pytest.raises(EstimateInputError):cost(source(rate='999999999999999999'),'2')
    with pytest.raises(EstimateInputError):_check(Fraction(1<<1024))
    with pytest.raises(EstimateInputError):_check(Fraction(1,1<<1024))


def test_invalid_count_not_hidden_by_missing_inputs():
    with pytest.raises(EstimateInputError):calculate(None,runs=None)
    with pytest.raises(EstimateInputError):calculate(source(),material_key='missing',runs=False)


def test_fractional_overhead_and_depreciation_exact():
    assert cost(source(printer_cost='1',printer_lifespan='3',monthly_rent='1',parts_per_month='3'),hours='1',runs=3).amount=='2.00'


def test_material_map_bounds_and_selected_key_type():
    bundle=source();bundle['material_rates']={str(i):dict(currency='USD',cost_per_gram='0') for i in range(129)}
    with pytest.raises(EstimateInputError):cost(bundle)
    with pytest.raises(EstimateInputError):calculate(source(),material_key=[])


def test_exact_components_reproduce_formula_and_rounding():
    result=cost(source(rate='0.005',printer_cost='1',printer_lifespan='3',monthly_rent='1',parts_per_month='3'),'1','1',3)
    records={record.name:record for record in result.components}
    values={key:Fraction(record.numerator,record.denominator) for key,record in records.items()}
    assert values['material']==Fraction(1,200)
    assert values['depreciation']==values['overhead']==Fraction(1,3)
    assert values['total']==Fraction(403,200) and result.amount=='2.02' and result.runs==3
    assert values['subtotal']*result.runs==values['total']
    assert all(record.currency=='USD' and record.basis==('all_runs' if record.name=='total' else 'per_run') for record in result.components)
    assert set(records)=={'material','labor','electricity','depreciation','packaging','other','base','failure','overhead','subtotal','total'}
    with pytest.raises(FrozenInstanceError):result.components[0].numerator=0
    with pytest.raises(FrozenInstanceError):result.components=()
    assert calculate(None).components==() and calculate(None).runs is None

@pytest.mark.parametrize('currency,fraction',[('USD','0.995'),('JPY','0.5')])
def test_rounded_boundary_rejected_even_raw_below_limit(currency,fraction):
    with pytest.raises(EstimateInputError,match='Rounded total'):
        cost(source(currency,packaging_cost='999999999999999999',other_costs=fraction))
    safe='0.994' if currency=='USD' else '0.499'
    result=cost(source(currency,packaging_cost='999999999999999999',other_costs=safe))
    assert Fraction(result.amount)<10**18
