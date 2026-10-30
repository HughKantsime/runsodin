"""Read advice and owner-confirmed start; normal dispatch remains authoritative."""
from typing import Literal, Any
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role, require_action, check_org_access
from modules.printers.models import Printer
from core.interfaces.completion import next_suggestion
from modules.printers.start_next import start, load, replay, request_key
from core.private_routes import PrivateRoute

class StartNextRoute(PrivateRoute):
    invalid_detail = 'Invalid start-next request. Check the job, fingerprint, request id and bed confirmation.'


router = APIRouter(route_class=StartNextRoute)


class StartNext(BaseModel):
    model_config = ConfigDict(extra='forbid')
    suggested_job_id: int = Field(gt=0, strict=True)
    suggestion_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    request_id: UUID
    bed_clear_confirmed: StrictBool
    attestation: Literal['owner_in_app', 'owner_confirmed_to_operator'] | None = None


class Suggestion(BaseModel):
    job_id: int | None
    reason: str
    filament: Literal['no_change', 'change_needed', 'verify_needed']
    fingerprint: str | None
    load: list[dict[str, Any]] = Field(default_factory=list)
    finished_attempt_uid: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


class StartResult(BaseModel):
    success: bool
    state: Literal['preparing', 'admitted', 'dispatched', 'refused', 'uncertain']
    job_id: int
    request_id: str
    message: str | None = None
    code: str | None = None
    accepted: bool = False
    attempt_id: str | None = None
    status_url: str | None = None


def visible_printer(db, printer_id, user):
    printer = db.get(Printer, printer_id)
    if not printer or (not check_org_access(user, printer.org_id) and not printer.shared):
        raise HTTPException(404, 'Printer not found')
    return printer


@router.get('/printers/{printer_id}/next-suggestion', tags=['Printers'], response_model=Suggestion)
def get_next_suggestion(printer_id: int = Path(gt=0), db: Session = Depends(get_db),
                        current_user: dict = Depends(require_role('viewer', scope='read:printers'))):
    return next_suggestion(db, visible_printer(db, printer_id, current_user),
                           visible=lambda job: check_org_access(current_user, job.charged_to_org_id))


@router.post('/printers/{printer_id}/start-next', tags=['Printers'], response_model=StartResult, status_code=202)
def start_next(data: StartNext, printer_id: int = Path(gt=0), db: Session = Depends(get_db),
               current_user: dict = Depends(require_action('jobs.start', scope='write:jobs')), wait: bool = False):
    prior = load(db, request_key(data.request_id, current_user))
    if prior is not None:
        result = replay(prior, current_user, printer_id)
    else:
        result = start(db, visible_printer(db, printer_id, current_user), current_user, data, background=not wait)
    if wait:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=200, content=result)
    return result
