"""Atomic, tenant-scoped and replay-safe shelf imports."""
import hashlib
import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from core.db import get_db
from core.models import SystemConfig
from core.rbac import require_action, get_org_scope
from modules.inventory.receiving import BulkReceive, lock_receiving, validate_references, receive_row
from .spools import _create_spool_record

router = APIRouter(prefix='/spools', tags=['Spools'])


@router.post('/receive/bulk')
def receive_bulk(body: BulkReceive,
                 current_user=Depends(require_action('spools.edit', scope='write:spools')),
                 db: Session = Depends(get_db)):
    digest = hashlib.sha256(json.dumps([row.model_dump() for row in body.rows], sort_keys=True).encode()).hexdigest()
    key = 'inventory.import.' + hashlib.sha256(f'{get_org_scope(current_user)}:{body.import_id}'.encode()).hexdigest()
    try:
        lock_receiving(db)
        prior = db.get(SystemConfig, key)
        if prior is not None:
            if prior.value['digest'] != digest:
                raise HTTPException(409, 'Import id already used for different rows')
            result = prior.value['result']
            db.rollback()
            return result
        validate_references(db, body.rows)
        rows = [receive_row(db, current_user, row, _create_spool_record) for row in body.rows]
        result = {'import_id': body.import_id, 'rows': rows, 'spools': [s for row in rows for s in row['spools']]}
        db.add(SystemConfig(key=key, value={'digest': digest, 'result': result}))
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
