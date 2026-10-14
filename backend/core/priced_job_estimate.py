"""Pure prospective estimated cost arithmetic; no authority or persistence proof."""
from dataclasses import dataclass
from fractions import Fraction
import re

CURRENCIES=('USD','EUR','GBP','CAD','AUD','JPY','CHF','NZD')
FIELDS=('spool_cost','spool_weight','post_processing_min','packing_min','support_min',
        'hourly_rate','printer_wattage','electricity_rate','printer_cost',
        'printer_lifespan','packaging_cost','other_costs','failure_rate',
        'monthly_rent','parts_per_month')
DECIMAL=re.compile(r'(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{0,8}[1-9])?\Z')

_MISSING=object()

class EstimateInputError(ValueError):
    """Provided assumptions are outside the closed arithmetic contract."""

@dataclass(frozen=True)
class ExactComponent:
    name: str
    currency: str
    basis: str
    numerator: int
    denominator: int

@dataclass(frozen=True)
class PricedEstimate:
    classification: str
    currency: str | None = None
    amount: str | None = None
    reason: str | None = None
    formula: str = 'per-run-cost-v1'
    components: tuple[ExactComponent, ...] = ()
    runs: int | None = None


def _check(value):
    if value.numerator.bit_length()>1024 or value.denominator.bit_length()>1024:
        raise EstimateInputError('Rational bound exceeded')
    return value


def _decimal(value, *, positive=False, maximum=None):
    if type(value) is not str or not DECIMAL.fullmatch(value):
        raise EstimateInputError('Canonical decimal string required')
    result=_check(Fraction(value))
    if (positive and result<=0) or (maximum is not None and result>maximum):
        raise EstimateInputError('Decimal bound exceeded')
    return result


def _round(value, digits):
    scaled=_check(value*10**digits)
    whole,remainder=divmod(scaled.numerator,scaled.denominator)
    if remainder*2>scaled.denominator or (remainder*2==scaled.denominator and whole%2):
        whole+=1
    if digits==0:return str(whole)
    return str(whole//100)+'.'+str(whole%100).zfill(2)


def calculate_priced_job_estimate(source, *, material_key=None, grams_per_run=None, hours_per_run=None, runs=_MISSING):
    """Return an explicitly unknown result for absent inputs, never default money.

    A known result proves arithmetic only. Its source ownership/revision and job
    provenance must be established separately by future transaction integration.
    """
    if runs is not _MISSING and (type(runs) is not int or not 1<=runs<=10000):
        raise EstimateInputError('Invalid run count')
    if source is None:return PricedEstimate('unknown',reason='missing_source')
    if type(source) is not dict or set(source)!={'currency','assumptions','material_rates'}:
        raise EstimateInputError('Closed source shape required')
    currency=source['currency']
    if type(currency) is not str or currency not in CURRENCIES:
        raise EstimateInputError('Unsupported currency')
    assumptions=source['assumptions']
    if type(assumptions) is not dict or set(assumptions)!=set(FIELDS):
        raise EstimateInputError('Complete assumptions required')
    a={k:_decimal(v,positive=k in ('spool_weight','printer_lifespan','parts_per_month'),
                  maximum=10000 if k=='failure_rate' else None) for k,v in assumptions.items()}
    rates=source['material_rates']
    if type(rates) is not dict or len(rates)>128:
        raise EstimateInputError('Bounded material map required')
    parsed={}
    for key,rate in rates.items():
        if type(key) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',key):
            raise EstimateInputError('Invalid material key')
        if type(rate) is not dict or set(rate)!={'currency','cost_per_gram'} or rate['currency']!=currency:
            raise EstimateInputError('Material currency mismatch')
        parsed[key]=_decimal(rate['cost_per_gram'])
    if material_key is not None and type(material_key) is not str:
        raise EstimateInputError('Invalid selected material key')
    if material_key is None or material_key not in parsed:
        return PricedEstimate('unknown',reason='missing_material')
    if grams_per_run is None or hours_per_run is None:
        return PricedEstimate('unknown',reason='missing_geometry')
    if runs is _MISSING:return PricedEstimate('unknown',reason='missing_run_count')
    grams=_decimal(grams_per_run,maximum=10**9)
    hours=_decimal(hours_per_run,maximum=10**9)
    material=_check(grams*parsed[material_key])
    labor=_check(_check(sum((a[k] for k in ('post_processing_min','packing_min','support_min')),Fraction()))/60*a['hourly_rate'])
    electricity=_check(a['printer_wattage']/1000*hours*a['electricity_rate'])
    depreciation=_check(a['printer_cost']/a['printer_lifespan']*hours)
    base=_check(material+labor+electricity+depreciation+a['packaging_cost']+a['other_costs'])
    failure=_check(base*a['failure_rate']/100)
    overhead=_check(a['monthly_rent']/a['parts_per_month'])
    per_run=_check(base+failure+overhead)
    total=_check(per_run*runs)
    if total>=10**18:raise EstimateInputError('Total bound exceeded')
    amount=_round(total,0 if currency=='JPY' else 2)
    if Fraction(amount)>=10**18:raise EstimateInputError('Rounded total bound exceeded')
    values=(('material',material),('labor',labor),('electricity',electricity),
            ('depreciation',depreciation),('packaging',a['packaging_cost']),
            ('other',a['other_costs']),('base',base),('failure',failure),
            ('overhead',overhead),('subtotal',per_run),('total',total))
    components=tuple(ExactComponent(name,currency,'all_runs' if name=='total' else 'per_run',
                                    value.numerator,value.denominator) for name,value in values)
    return PricedEstimate('job_estimate',currency,amount,components=components,runs=runs)
