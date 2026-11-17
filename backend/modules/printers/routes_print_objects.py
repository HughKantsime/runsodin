"""Read the authorized object list without changing printer or database state."""
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role, require_scope
from modules.printers.print_objects import running_print_objects
from modules.printers.routes_status import _visible_printer

router = APIRouter()


@router.get('/printers/{printer_id}/print-objects', tags=['Printers'])
def get_print_objects(
    printer_id: int,
    response: Response,
    current_user: dict = Depends(require_role('viewer', scope='read:printers')),
    _models_scope: dict = Depends(require_scope('read:models')),
    db: Session = Depends(get_db),
):
    printer = _visible_printer(printer_id, db, current_user)
    response.headers['Cache-Control'] = 'no-store'
    return running_print_objects(db, printer, current_user)
