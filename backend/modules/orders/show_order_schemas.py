"""Operator-entered facts, never card details or provider verification."""
from datetime import date,datetime
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,StrictBool,StrictInt,field_validator

class Address(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    recipient:str=Field(min_length=1,max_length=160)
    line1:str=Field(min_length=1,max_length=200)
    line2:str=Field(default='',max_length=200)
    city:str=Field(min_length=1,max_length=100)
    region:str=Field(min_length=1,max_length=100)
    postal_code:str=Field(min_length=1,max_length=30)
    country:Literal['US']='US'

class Intake(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    revision:str|None=Field(default=None,max_length=64)
    correction_note:str=Field(default='',max_length=400)
    address:Address
    address_confirmed:StrictBool=False
    source:str=Field(min_length=1,max_length=160)
    dispatch_by:date
    dispatch_timezone:str=Field(default='America/New_York',max_length=64)

    @field_validator('dispatch_timezone')
    @classmethod
    def valid_zone(cls,value):
        from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
        try:ZoneInfo(value)
        except (ValueError,ZoneInfoNotFoundError):raise ValueError('Choose a known promise time zone') from None
        return value

    currency:Literal['USD']='USD'
    amount_received_minor:StrictInt=Field(ge=0,le=100000000)
    payment_method:Literal['square_external','cash','other_external']
    payment_reference:str=Field(default='',max_length=160)
    payment_received_at:datetime|None=None
    payment_confirmed:StrictBool=False

class Handoff(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    revision:str=Field(min_length=1,max_length=64)
    carrier:str=Field(min_length=1,max_length=80)
    tracking_number:str=Field(min_length=1,max_length=100)
    handed_at:datetime
    physically_handed_over:StrictBool
