"""License selection control plane (B1). No hardware reconciliation is enabled by this API."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, StrictInt
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_interactive_session, require_superadmin

router = APIRouter()


class LicenseSelectionInput(BaseModel):
    printers: list[StrictInt]
    users: list[StrictInt]
    expected_revision: StrictInt
    recovery_user_id: StrictInt | None = None


@router.get('/license/selection', tags=['License'], dependencies=[Depends(require_interactive_session())])
def get_license_selection(current_user: dict = Depends(require_superadmin(scope='read')), db: Session = Depends(get_db)):
    from core.license_lock import selection_status
    result = selection_status(db, read_only=True)
    return result


@router.put('/license/selection', tags=['License'], dependencies=[Depends(require_interactive_session())])
def put_license_selection(body: LicenseSelectionInput, current_user: dict = Depends(require_superadmin(scope='write')), db: Session = Depends(get_db)):
    from core.license_lock import replace_selection, status_view
    try:
        result = replace_selection(db, **body.model_dump(), actor=current_user['id'])
        from core.audit import log_audit
        log_audit(db, 'license.selection', 'system', details={'revision': result['revision'], 'actor_id': current_user['id']})
        db.commit()
        return status_view(result)
    except Exception:
        db.rollback()
        raise
