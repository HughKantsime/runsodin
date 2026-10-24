"""Storage receiving and tenant-owned material/color reorder totals."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session
from core.db import get_db
from core.base import SpoolStatus
from core.dependencies import log_audit
from core.errors import ErrorCode, OdinError
from core.models import SystemConfig
from core.rbac import require_action, require_role, get_org_scope, should_filter_org, _get_org_filter
from modules.inventory.models import Spool, FilamentLibrary
from modules.inventory.stock import stock_identity, policy_prefix, policy_key, seal_key
from ._helpers import SpoolCreate
from .spools import _create_spool_record

router = APIRouter(prefix='/spools', tags=['Spools'])


class ReceiveRolls(BaseModel):
    model_config = ConfigDict(extra='forbid')
    filament_id: int = Field(gt=0)
    quantity: int = Field(ge=1, le=100, strict=True)
    initial_weight_g: float = Field(default=1000, gt=0, le=100000, allow_inf_nan=False)
    spool_weight_g: float = Field(default=250, ge=0, le=10000, allow_inf_nan=False)
    storage_location: str = Field(min_length=1, max_length=100)
    sealed: bool = True
    vendor: str | None = Field(default=None, max_length=100)

    @field_validator('storage_location')
    @classmethod
    def location(cls, value):
        if not value.strip():
            raise ValueError('Storage location is required')
        return value.strip()


@router.post('/receive')
def receive_rolls(body: ReceiveRolls,
                  current_user=Depends(require_action('spools.edit', scope='write:spools')),
                  db: Session = Depends(get_db)):
    # One transaction including economic birth evidence and audit for every roll.
    if db.get(FilamentLibrary, body.filament_id) is None:
        raise OdinError(ErrorCode.not_found, 'Filament not found', status=404)
    results = []
    try:
        for _ in range(body.quantity):
            row = _create_spool_record(SpoolCreate(**body.model_dump(exclude={'quantity', 'sealed'})), current_user, db, commit=False)
            if body.sealed:
                db.add(SystemConfig(key=seal_key(row['id']), value=True))
            results.append(row)
        log_audit(db, 'inventory.receive', 'spools', details={'ids': [r['id'] for r in results], 'storage_location': body.storage_location, 'sealed': body.sealed})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {'spools': results}


class ReorderPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    material: str = Field(min_length=1, max_length=100)
    color: str = Field(min_length=1, max_length=100)
    threshold_g: int = Field(ge=0, le=10000000, strict=True)

    @field_validator('material', 'color')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Material and color are required')
        return value


@router.put('/reorder-policy')
def set_reorder_policy(body: ReorderPolicy,
                       current_user=Depends(require_action('spools.edit', scope='write:spools')),
                       db: Session = Depends(get_db)):
    material, color = stock_identity(body.material, body.color)
    # Writes always target the caller's org; an all-org view never creates a
    # policy that could overwrite another tenant's setting.
    org = get_org_scope(current_user)
    key = policy_key(org, material, color)
    value = dict(material=material, color=color, threshold_g=body.threshold_g)
    from sqlalchemy import text
    import json
    db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
               {'key': key, 'value': json.dumps(value)})
    log_audit(db, 'inventory.reorder_policy', 'spools', details={**value, 'org_id': org})
    db.commit()
    return value


@router.get('/reorder')
def reorder_rollup(org_id: int | None = None,
                   current_user=Depends(require_role('viewer', scope='read:spools')),
                   db: Session = Depends(get_db)):
    org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    query = db.query(Spool).filter(Spool.status == SpoolStatus.ACTIVE)
    if should_filter_org(current_user, org):
        query = query.filter((Spool.org_id == org) | Spool.org_id.is_(None))
    policies = {stock_identity(r.value['material'], r.value['color']): r.value['threshold_g']
                for r in db.query(SystemConfig).filter(SystemConfig.key.startswith(policy_prefix(org))).all()}
    seals = {r.key for r in db.query(SystemConfig).filter(SystemConfig.key.startswith('inventory.sealed.')).all() if r.value is True}
    groups = {}
    def group(key):
        return groups.setdefault(key, dict(material=key[0], color=key[1], sealed_rolls=0, sealed_g=0, open_g=0, total_g=0, threshold_g=policies.get(key, 1000)))
    for key in policies:
        group(key)  # Keep configured groups visible even at zero stock.
    for spool in query.all():
        filament = spool.filament
        key = stock_identity(filament.material if filament else None, spool.color_hex or (filament.color_hex if filament else None))
        row = group(key)
        grams = max(0, spool.remaining_weight_g or 0)
        sealed = seal_key(spool.id) in seals and spool.location_printer_id is None and grams == spool.initial_weight_g
        row['sealed_rolls'] += int(sealed)
        row['sealed_g' if sealed else 'open_g'] += grams
        row['total_g'] += grams
    return [{**row, 'reorder': row['total_g'] < row['threshold_g'], 'shortfall_g': max(0, row['threshold_g'] - row['total_g'])}
            for _, row in sorted(groups.items())]
