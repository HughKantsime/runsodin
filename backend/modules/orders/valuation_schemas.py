"""Closed private unit-input response; no arbitrary snapshot fields."""
from typing import Literal
from pydantic import BaseModel,ConfigDict,StrictInt,Field


class _Closed(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)


class UnitTarget(_Closed):
    owner_scope:Literal['org','global']
    owner_key:StrictInt
    job_uid:str=Field(min_length=36,max_length=36)
    item_uid:str=Field(min_length=36,max_length=36)
    order_uid:str=Field(min_length=36,max_length=36)
    currency:Literal['USD','EUR','GBP','CAD','AUD','JPY','NZD','CHF']|None


class UnitSource(_Closed):
    spool_uid:str|None=Field(default=None,max_length=36)
    birth_nonce:str|None=Field(default=None,max_length=32)
    state:Literal['known','unknown']
    reason:Literal['unproved_source_lifetime','foreign_source_owner','missing_purchase_evidence','missing_target_currency','currency_mismatch']|None
    evidence_nonce:str|None=Field(default=None,max_length=32)
    revision:StrictInt|None=Field(default=None,ge=1,le=2147483647)
    currency:Literal['USD','EUR','GBP','CAD','AUD','JPY','NZD','CHF']|None=None
    price_decimal:str|None=Field(default=None,max_length=31)
    net_weight_g_decimal:str|None=Field(default=None,max_length=31)
    weight_basis:Literal['purchased_net_filament_g']|None=None
    provenance:Literal['owner_attestation']|None=None


class PrivateUnitValuation(_Closed):
    version:Literal[2]
    state:Literal['known','unknown']
    reason:Literal['unresolved_material_mapping','invalid_bounded_sources','unresolved_source_valuation']|None
    attempt_uid:str=Field(min_length=36,max_length=36)
    attempt_birth:str=Field(min_length=32,max_length=32)
    captured_at:str=Field(max_length=27)
    target:UnitTarget
    sources:list[UnitSource]=Field(max_length=16)


class UnavailableValuation(_Closed):
    state:Literal['unavailable']
    reason:Literal['missing_historical_valuation']


MeasuredReason=Literal['missing_measurement','invalid_measurement','missing_historical_valuation','unresolved_allocation','unresolved_purchase_evidence','currency_mismatch','valuation_bound_exceeded','mixed_order_plate','archive_ownership_unresolved','open_attempt','missing_terminal_observation','no_participating_attempts','inventory_shortfall']

class MeasuredAttempt(_Closed):
    attempt_id:StrictInt|None
    outcome:Literal['completed','failed','cancelled','open']
    measurement_basis:Literal['printer_reported','unavailable','invalid']
    amount:str|None=Field(max_length=100)
    currency:Literal['USD','EUR','GBP','CAD','AUD','JPY','NZD','CHF']|None
    reason:MeasuredReason|None

class OrderMeasuredCosts(_Closed):
    measured_filament_known_subtotal:str|None=Field(max_length=100)
    currency:Literal['USD','EUR','GBP','CAD','AUD','JPY','NZD','CHF']|None
    filament_state:Literal['fully_measured','partial','estimated_only','unknown']
    production_cogs_state:Literal['unavailable']
    known_attempts:StrictInt=Field(ge=0)
    unresolved_attempts:StrictInt=Field(ge=0)
    open_attempts:StrictInt=Field(ge=0)
    reasons:list[MeasuredReason]
    successful_subtotal:str|None=Field(max_length=100)
    failed_cancelled_subtotal:str|None=Field(max_length=100)
    attempts:list[MeasuredAttempt]=Field(max_length=1000)
