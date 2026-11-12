"""GET /printers/{id}/current-plate-thumbnail: the plate picture of the print running on a printer.

Served only from the plate cache and only to a caller who may read the printer, the running print
and its file (core.model_preview_access.accessible_preview_file: organization, controlled-part
audience, Education policy). It is deliberately not part of the printer status payload, WebSocket or
MQTT events, the overlay, the Home Assistant payload or any camera-token route: a page that does not
call this endpoint cannot receive the image.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.responses import Response

from core.db import get_db
from core.dispatch_payload_selection import SELECTED_PRINT_FILE_ID_SQL
from core.rbac import require_role, require_scope

router = APIRouter()

_RUNNING_PRINT = text(
    "SELECT id, scheduled_job_id FROM print_jobs WHERE printer_id = :pid AND status = 'running' "
    "ORDER BY started_at DESC, id DESC LIMIT 1")
# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed SQL plus the shared source-owned selection fragment; the job id is bound
_JOB_FILE = text(
    "SELECT " + SELECTED_PRINT_FILE_ID_SQL + " AS file_id, j.plate_index AS plate_index FROM jobs j "
    "LEFT JOIN models m ON m.id = j.model_id LEFT JOIN education_submissions s ON s.job_id = j.id "
    "WHERE j.id = :job ORDER BY s.id LIMIT 1")


def _not_found():
    return HTTPException(status_code=404, detail="Plate picture not available")


@router.get("/printers/{printer_id}/current-plate-thumbnail", tags=["Printers"])
def get_current_plate_thumbnail(
    printer_id: int,
    current_user: dict = Depends(require_role("viewer", scope="read:printers")),
    _models_scope: dict = Depends(require_scope("read:models")),
    db: Session = Depends(get_db),
):
    from core.controlled_parts import allowed
    from core.model_preview_access import accessible_preview_file
    from modules.models_library import plate_cache
    from modules.printers.routes_status import _visible_printer

    _visible_printer(printer_id, db, current_user)              # same visibility as the printer status routes
    running = db.execute(_RUNNING_PRINT, {"pid": printer_id}).first()
    if running is None or running.scheduled_job_id is None:
        raise _not_found()
    selected = db.execute(_JOB_FILE, {"job": running.scheduled_job_id}).first()
    if selected is None or selected.file_id is None:
        raise _not_found()
    # Pure check first (no audit row per poll); also covers a marked job whose file is unmarked.
    if not allowed(db, "print_job", running.id, current_user):
        raise _not_found()
    try:
        row = accessible_preview_file(db, selected.file_id, current_user)
    except HTTPException:
        raise _not_found() from None                           # outside the audience looks like "none"
    plate = int(selected.plate_index or 0) + 1                  # jobs.plate_index is zero-based; default plate 1
    image = plate_cache.thumbnail_for_row(row, plate)
    if image is None:
        # A file stored before this feature has no cache yet: build it in the background (this
        # request never waits for a parse), so the next poll has the picture.
        plate_cache.build_in_background(row)
        raise _not_found()
    return Response(content=image.content, media_type=image.media_type,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
