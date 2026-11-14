"""Per-plate facts and thumbnails of a stored print file (the derived plate cache)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from core.audit import audit_download
from core.db import get_db
from core.rbac import require_role

router = APIRouter()


@router.get("/print-files/{file_id}/plates", tags=["Print Files"])
def get_print_file_plates(file_id: int, current_user: dict = Depends(require_role("viewer", scope="read:models")), db: Session = Depends(get_db)):
    """Per-plate facts of a sliced 3MF (no thumbnails inline). Authorized exactly like the mesh preview:
    organization, controlled-part audience and Education policy. Built from the stored file on first read."""
    from core.model_preview_access import accessible_preview_file
    from modules.models_library import plate_cache
    from modules.models_library.mesh_isolation import Busy
    row = accessible_preview_file(db, file_id, current_user)
    try:
        facts = plate_cache.facts_for_row(row)
    except Busy as busy:
        raise HTTPException(status_code=429, detail="3MF analysis is busy; try again shortly",
                            headers={"Retry-After": str(busy.retry_after)}) from None
    if facts is None:
        raise HTTPException(status_code=404, detail="Plate data not available")
    return facts


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
