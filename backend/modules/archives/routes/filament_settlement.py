"""Visible archive accounting reads and an atomic operator settlement."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
from core.db import get_db
from core.rbac import require_action, require_role
from core.order_production import ProductionLinkageChanged
from modules.archives.access import archive_visibility
from modules.archives.filament_settlement import Settlement, archive_row, settle
from modules.archives.settlement_data import preview
from modules.archives.accounting_causes import describe

router = APIRouter()


@router.get('/archives/accounting-summary', tags=['Archives'])
def accounting_summary(days: int = Query(30, ge=1, le=365),
    user=Depends(require_role('viewer', scope='read:jobs')), db: Session=Depends(get_db)):
    audience, bindings = archive_visibility(user)
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- trusted archive visibility fragments; all values bound
    rows = db.execute(text("SELECT a.accounting_reason,a.ownership_source,a.completed_at FROM print_archives a "
        "WHERE a.accounting_state='unresolved' AND a.completed_at>=:since AND a.completed_at<=:now AND (" + audience + ')'),
        dict(bindings, since=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days),
             now=datetime.now(timezone.utc).replace(tzinfo=None))).all()
    causes = Counter((r[0], describe(r[0], r[1])['title']) for r in rows)
    return dict(unresolved=len(rows), by_cause=[dict(code=code, title=title, count=count)
                for (code, title), count in sorted(causes.items(), key=lambda pair: (-pair[1], str(pair[0])))],
                oldest_completed_at=min((r[2] for r in rows), default=None))


@router.get('/archives/{archive_id}/filament-settlement', tags=['Archives'])
def filament_settlement(archive_id: int, spool_id: list[int] = Query(default=[], max_length=16),
    user=Depends(require_role('viewer', scope='read:jobs')), db: Session=Depends(get_db)):
    return preview(db, archive_row(db, archive_id, user), user, spool_id)


@router.post('/archives/{archive_id}/settle-filament', tags=['Archives'])
def settle_filament(archive_id: int, body: Settlement,
    user=Depends(require_action('archives.settle_filament', scope='write:spools')), db: Session=Depends(get_db)):
    try:
        result = settle(db, archive_id, body, user)
        db.commit()
        return result
    except ProductionLinkageChanged:
        db.rollback()
        raise HTTPException(409, 'The job linked to this print changed. Refresh and try again.') from None
    except OperationalError as exc:
        db.rollback()
        if getattr(exc.orig, 'sqlstate', None) in ('55P03', '40P01') or getattr(exc.orig, 'sqlite_errorcode', None) in (5, 6, 517):
            raise HTTPException(409, 'Filament records are busy. Refresh and try again.') from None
        raise
    except Exception:
        db.rollback()
        raise
