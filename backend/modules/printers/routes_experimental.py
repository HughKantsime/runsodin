"""Experimental printer types: the superadmin opt-in route and the control guard.

``experimental_controls_guard`` is attached to the whole controls router in routes.py,
so a control route added there later is refused for an unopted Experimental printer by default.
"""
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user
from core.rbac import check_org_access, require_superadmin
from modules.printers import support_levels
from modules.printers.models import Printer

router = APIRouter(tags=['Printers'])

# Queue bookkeeping (plate-cleared) is always allowed. Safety actions (pause, stop)
# bypass opt-in only for types that implement controls, never monitoring-only types.
_EXEMPT_SUFFIXES = frozenset({'plate-cleared', 'pause', 'stop'})
_SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})


class ExperimentalActions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: StrictBool  # "yes" or 1 is refused (422), never read as true


@router.put('/printers/{printer_id}/experimental-actions')
def set_experimental_actions(
    body: ExperimentalActions,
    printer_id: int = Path(gt=0),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_superadmin('write:printers')),
):
    """Turn job sending and controls on or off for one Experimental printer (platform superadmin)."""
    from core.audit import log_audit
    try:
        printer = db.get(Printer, printer_id)
        if printer is None:
            raise HTTPException(404, 'Printer not found')
        level = support_levels.support_level(printer.api_type)
        if body.enabled and not support_levels.opt_in_applies(printer.api_type):
            raise HTTPException(409, detail={
                'code': 'not_experimental',
                'message': 'Only a printer of an Experimental type has this setting.'})
        # Same printer row dispatch admission locks: an opt-in change cannot interleave with an admission.
        db.execute(text('UPDATE printers SET is_active=is_active WHERE id=:id'), {'id': printer_id})
        db.expire_all()
        printer = db.get(Printer, printer_id)
        if printer is None:
            raise HTTPException(404, 'Printer not found')
        result = support_levels.set_experimental_enabled(db, printer, body.enabled, current_user.get('id'))
        if result['changed']:
            log_audit(db, 'printer.experimental_actions_enabled' if body.enabled else 'printer.experimental_actions_disabled',
                      'printer', printer_id,
                      {'api_type': support_levels.normalize(printer.api_type), 'support_level': level,
                       'actor_token_id': current_user.get('_token_id')})
        db.commit()
        return {'printer_id': printer_id, 'support_level': level,
                'experimental_actions_enabled': result['enabled'] and support_levels.opt_in_applies(printer.api_type)}
    except Exception:
        db.rollback()
        raise


def experimental_controls_guard(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Refuse any non-read request on the controls router for an unopted Experimental printer.

    A caller without access to the printer is left to the handler's own 404/403.
    """
    if request.method in _SAFE_METHODS:
        return
    route = request.scope.get('route')
    action = (getattr(route, 'path', '') or '').rsplit('/', 1)[-1]
    if action == 'plate-cleared':
        return
    try:
        printer_id = int(request.path_params.get('printer_id'))
    except (TypeError, ValueError):
        return
    printer = db.get(Printer, printer_id)
    if printer is None or not check_org_access(current_user, printer.org_id):
        return
    if action in _EXEMPT_SUFFIXES and not support_levels.is_monitor_only(printer.api_type):
        return
    refusal = support_levels.control_refusal(db, printer.id, printer.api_type, action)
    if refusal is None:
        return
    from core.audit import log_audit
    log_audit(db, 'printer.experimental_control_refused', 'printer', printer.id,
              {'action': action, 'api_type': support_levels.normalize(printer.api_type),
               'actor_token_id': current_user.get('_token_id')})
    db.commit()
    code, _, message = refusal.partition(': ')
    raise HTTPException(409, detail={'code': code, 'message': message})


# A recycled printer id must never inherit another printer's opt-in.
@event.listens_for(Printer, 'after_delete')
def _forget_experimental_opt_in(mapper, connection, target):
    support_levels.forget_printer(connection, target.id)
