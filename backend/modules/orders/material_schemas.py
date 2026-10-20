"""Counted hardware, explicit receipt facts and bounded structured import."""
from datetime import date,datetime
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,StrictInt,StrictBool

class Input(BaseModel):model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
class MaterialInput(Input):
    name:str=Field(min_length=1,max_length=160)
    specification:str=Field(min_length=1,max_length=300)
    unit:Literal['piece']='piece'
class AdoptInput(Input):
    consumable_id:StrictInt=Field(gt=0)
    specification:str=Field(min_length=1,max_length=300)
    counted_units:StrictInt=Field(ge=0,le=100000000)
    physically_counted:StrictBool
class MappingInput(Input):
    consumable_id:StrictInt=Field(gt=0)
    vendor:str=Field(min_length=1,max_length=60)
    listing_id:str=Field(min_length=1,max_length=120)
    variant:str=Field(min_length=1,max_length=160)
    specification:str=Field(min_length=1,max_length=300)
    units_per_pack:StrictInt=Field(gt=0,le=100000)
    verified:StrictBool
class PurchaseLine(Input):
    vendor:str=Field(min_length=1,max_length=60)
    order_reference:str=Field(min_length=1,max_length=120)
    line_reference:str=Field(min_length=1,max_length=100)
    listing_id:str=Field(min_length=1,max_length=120)
    variant:str=Field(min_length=1,max_length=160)
    specification:str=Field(min_length=1,max_length=300)
    packs:StrictInt=Field(gt=0,le=10000)
    units_per_pack:StrictInt=Field(gt=0,le=100000)
    total_cost_minor:StrictInt|None=Field(default=None,ge=0,le=100000000)
    expected_on:date|None=None
    source_reference:str=Field(default='',max_length=300)
class PurchaseInput(Input):lines:list[PurchaseLine]=Field(min_length=1,max_length=50)
class CSVInput(Input):csv_text:str=Field(min_length=1,max_length=100000)
class ReceiptInput(Input):
    request_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    line_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    source_event_reference:str=Field(min_length=1,max_length=120)
    kind:Literal['receive_good','receive_damaged','cancel','return_good','return_damaged']
    units:StrictInt=Field(gt=0,le=100000000)
    occurred_at:datetime
    confirmed:StrictBool
    note:str=Field(default='',max_length=300)
class HardwareUse(Input):
    consumable_id:StrictInt=Field(gt=0)
    units:StrictInt=Field(ge=0,le=100000000)
    reason:str=Field(default='',max_length=300)
