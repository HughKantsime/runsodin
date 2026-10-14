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
