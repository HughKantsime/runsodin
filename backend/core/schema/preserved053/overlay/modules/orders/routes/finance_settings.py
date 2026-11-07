"""Scoped admin currency configuration; never assigns historical currency."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role, require_action
from core.finance_currency import read_currency, write_currency

router = APIRouter(prefix='/orders', tags=['orders'])
Currency = Literal['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF', 'NZD']


class CurrencyUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    currency: Currency | None


def finance_owner(user, org_id=None, global_scope=False):
    group = user.get('group_id')
    if global_scope and org_id is not None:
        raise HTTPException(422, 'Select either organization or global scope')
    if group is None and user.get('role') == 'admin':
        if global_scope:
            return 'global', 0
        if type(org_id) is int and org_id > 0:
            return 'org', org_id
        raise HTTPException(422, 'Explicit finance scope selection required')
    if type(group) is not int or group <= 0:
        raise HTTPException(403, 'Positive organization authority required')
    if global_scope or (org_id is not None and org_id != group):
        raise HTTPException(403, 'Finance scope unavailable')
    return 'org', group


def _result(scope, key, currency):
    return {'owner_scope': scope, 'owner_key': key, 'currency': currency}


@router.get('/finance-settings')
def get_finance_settings(org_id: int | None = Query(None, gt=0), global_scope: bool = False,
                         current_user: dict = Depends(require_role('admin', scope='read')),
                         db: Session = Depends(get_db)):
    scope, key = finance_owner(current_user, org_id, global_scope)
    return _result(scope, key, read_currency(db, owner_scope=scope, owner_key=key))


@router.put('/finance-settings')
def put_finance_settings(data: CurrencyUpdate, org_id: int | None = Query(None, gt=0),
                         global_scope: bool = False,
                         current_user: dict = Depends(require_action("settings.edit", scope='admin')),
                         db: Session = Depends(get_db)):
    scope, key = finance_owner(current_user, org_id, global_scope)
    try:
        currency = write_currency(db, owner_scope=scope, owner_key=key, currency=data.currency)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return _result(scope, key, currency)
