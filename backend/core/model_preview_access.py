"""Shared authorization boundary for original files and derived preview geometry."""
from fastapi import HTTPException
from sqlalchemy import text
from core.rbac import check_org_access
from core.registry import registry


def preview_not_found():
    return HTTPException(status_code=404, detail="Preview not found")


def accessible_preview_file(db, file_id, principal):
    row = db.execute(text(
        "SELECT id,org_id,stored_path,original_filename,mesh_data FROM print_files WHERE id=:id"
    ), {"id": file_id}).fetchone()
    if not row or not check_org_access(principal, row.org_id):
        raise preview_not_found()
    owned = db.execute(text(
        "SELECT 1 FROM education_submissions WHERE print_file_id=:id "
        "UNION ALL SELECT 1 FROM education_upload_operations "
        "WHERE final_path=:path AND final_path IS NOT NULL LIMIT 1"
    ), {"id": file_id, "path": row.stored_path}).fetchone()
    if owned:
        provider = registry.get_provider("EducationPolicyProvider")
        authorize = getattr(provider, "assert_file_preview_allowed", None)
        if not callable(authorize):
            raise preview_not_found()
        try:
            authorize(db, file_id=file_id, principal=principal)
        except Exception:
            # An unavailable or rejecting authority must never become generic access.
            raise preview_not_found() from None
    return row
