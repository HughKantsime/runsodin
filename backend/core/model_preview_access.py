"""Shared authorization boundary for original files and derived preview geometry."""
from fastapi import HTTPException
from sqlalchemy import text
from core.rbac import check_org_access
from core.registry import registry


def preview_not_found():
    return HTTPException(status_code=404, detail="Preview not found")


def is_education_owned_file(db, file_id, stored_path):
    """Persisted ownership remains protective even when the policy provider is absent."""
    return db.execute(text(
        "SELECT 1 FROM education_submissions WHERE print_file_id=:id "
        "UNION ALL SELECT 1 FROM education_upload_operations "
        "WHERE final_path=:path AND final_path IS NOT NULL LIMIT 1"
    ), {"id": file_id, "path": stored_path}).fetchone() is not None


def delete_generic_file_if_unchanged(db, row, *, parent_org_id, require_unreferenced=False):
    """Delete only the authorized identity, with persisted protection checked in the write."""
    result = db.execute(text(
        "DELETE FROM print_files WHERE id=:id "
        "AND (org_id=:org OR (org_id IS NULL AND CAST(:org AS INTEGER) IS NULL)) "
        "AND (model_id=:model OR (model_id IS NULL AND CAST(:model AS INTEGER) IS NULL)) "
        "AND (stored_path=:path OR (stored_path IS NULL AND CAST(:path AS TEXT) IS NULL)) "
        "AND (CAST(:model AS INTEGER) IS NULL OR EXISTS (SELECT 1 FROM models m WHERE m.id=print_files.model_id "
        "AND (m.org_id=:parent_org OR (m.org_id IS NULL AND CAST(:parent_org AS INTEGER) IS NULL)))) "
        "AND (:protect=0 OR (job_id IS NULL AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.print_file_id=print_files.id))) "
        "AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.print_file_id=print_files.id) "
        "AND NOT EXISTS (SELECT 1 FROM education_upload_operations o "
        "WHERE o.final_path=print_files.stored_path AND o.final_path IS NOT NULL)"
    ), {"id": row.id, "org": row.org_id, "model": row.model_id, "path": row.stored_path,
        "parent_org": parent_org_id, "protect": int(require_unreferenced)})
    return result.rowcount == 1


def accessible_preview_file(db, file_id, principal):
    row = db.execute(text(
        "SELECT id,org_id,stored_path,original_filename,mesh_data,filaments_json,"
        "model_id,filename,printer_model,print_time_seconds,total_weight_grams,project_name,job_id,"
        "file_hash,created_by,layer_count,supports_used,bed_type,thumbnail_b64,filament_weight_grams,"
        "storage_bytes,blob_state,compatibility_facts_json,"
        "nozzle_diameter,layer_height,uploaded_at,bed_x_mm,bed_y_mm,compatible_api_types,plate_count "
        "FROM print_files WHERE id=:id"
    ), {"id": file_id}).fetchone()
    if not row or not check_org_access(principal, row.org_id):
        raise preview_not_found()
    owned = is_education_owned_file(db, file_id, row.stored_path)
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
