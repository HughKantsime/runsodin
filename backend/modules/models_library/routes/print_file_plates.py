"""Per-plate facts and thumbnails of a stored print file (the derived plate cache)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.audit import audit_download
from core.db import get_db
from core.rbac import require_action, require_role, require_superadmin

router = APIRouter()


@router.get("/print-files/{file_id}/plates", tags=["Print Files"])
def get_print_file_plates(file_id: int, current_user: dict = Depends(require_role("viewer", scope="read:models")), db: Session = Depends(get_db)):
    """Per-plate facts of a stored 3MF or G-code file (no thumbnails inline). Authorized exactly like the mesh preview:
    organization, controlled-part audience and Education policy. Built from the stored file on first read."""
    from core.model_preview_access import accessible_preview_file
    from modules.models_library import plate_cache
    from modules.models_library.mesh_isolation import Busy
    row = accessible_preview_file(db, file_id, current_user)
    try:
        facts, reason = plate_cache.facts_result(row)
    except Busy as busy:
        raise HTTPException(status_code=429, detail="3MF analysis is busy; try again shortly",
                            headers={"Retry-After": str(busy.retry_after)}) from None
    if facts is None:
        return {"status": "not_available", "reason": reason, "file_hash": row.file_hash}
    return {"status": "available", **facts}


@router.get("/print-files/{file_id}/plates/{plate}/thumbnail", tags=["Print Files"])
def get_print_file_plate_thumbnail(file_id: int, plate: int, current_user: dict = Depends(require_role("viewer", scope="read:models")), db: Session = Depends(get_db)):
    """One plate's picture from the cache (404 when absent). Same authorization as the facts route."""
    from starlette.responses import Response
    from core.model_preview_access import accessible_preview_file
    from modules.models_library import plate_cache
    row = accessible_preview_file(db, file_id, current_user)
    image = plate_cache.thumbnail_for_row(row, plate)
    if image is None:
        raise HTTPException(status_code=404, detail="Plate thumbnail not available")
    audit_download(db, "print_file.downloaded", "print_file", file_id, {"what": "plate_thumbnail"})
    return Response(content=image.content, media_type=image.media_type,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/print-files/{file_id}/completed-jobs", tags=["Print Files"],
            dependencies=[Depends(require_role("viewer", scope="read:jobs"))])
def list_completed_jobs(file_id: int, current_user=Depends(require_role("viewer", scope="read:models")),
                        db: Session = Depends(get_db)):
    from core.model_preview_access import accessible_preview_file
    from modules.models_library.library_search import completed_file_jobs
    accessible_preview_file(db, file_id, current_user)
    return completed_file_jobs(db, current_user, file_id)


@router.get("/print-files/plates/backfill", tags=["Print Files"])
def backfill_status(current_user=Depends(require_superadmin(scope="read:models"))):
    from modules.models_library.plate_backfill import status
    return status()


@router.post("/print-files/plates/backfill", tags=["Print Files"],
             dependencies=[Depends(require_action("models.backfill", scope="write:models"))])
def start_backfill(current_user=Depends(require_superadmin(scope="write:models")), db=Depends(get_db)):
    from modules.models_library.plate_backfill import start
    return start(db, current_user)
