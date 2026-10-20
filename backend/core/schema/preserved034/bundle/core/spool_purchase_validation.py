"""Strict explicit purchase inputs; floats/default operational weights are not evidence."""
from decimal import Decimal
import re
from pydantic import BaseModel,ConfigDict,field_validator
from core.finance_currency import SUPPORTED_CURRENCIES
MAX_REVISION=2147483647
_INPUT=re.compile(r'[0-9]{1,18}(?:\.[0-9]{1,12})?',re.ASCII)
_CANONICAL=re.compile(r'(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{0,11}[1-9])?',re.ASCII)

def decimal_input(value,*,positive=False):
    if type(value) is not str or _INPUT.fullmatch(value) is None:raise ValueError('Purchase decimal requires a bounded plain decimal string')
    number=Decimal(value)
    if not number.is_finite() or number<0 or (positive and number==0):raise ValueError('Purchase decimal is outside bounds')
    result=format(number,'f')
    if '.' in result:result=result.rstrip('0').rstrip('.')
    return '0' if number==0 else result

def canonical_decimal(value,*,positive=False):
    return type(value) is str and _CANONICAL.fullmatch(value) is not None and (not positive or value!='0')

class PurchaseInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    currency:str
    price:str
    net_weight_g:str
    @field_validator('currency',mode='before')
    @classmethod
    def currency_input(cls,value):
        if type(value) is not str or value not in SUPPORTED_CURRENCIES:raise ValueError('Unsupported purchase currency')
        return value
    @field_validator('price',mode='before')
    @classmethod
    def price_input(cls,value):return decimal_input(value)
    @field_validator('net_weight_g',mode='before')
    @classmethod
    def weight_input(cls,value):return decimal_input(value,positive=True)
