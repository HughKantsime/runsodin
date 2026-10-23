"""Packaged exact Education archive projection predicates; no I/O or authority inference."""

_BASE_SELECT = (
    "SELECT s.id,s.org_id,s.job_id,s.print_file_id,s.cost_center_id,s.submitted_by,"
    "s.status,s.approved_printer_id,s.approved_by,s.lifecycle_revision,"
    "s.compatibility_engine_version,s.created_at,s.updated_at,c.state AS center_state,"
    "j.status AS job_status,j.item_name,j.rejected_reason,u.username AS submitter_username "
    "FROM education_submissions s "
    "JOIN education_cost_centers c ON c.id=s.cost_center_id AND c.org_id=s.org_id "
    "JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
    "JOIN education_memberships em ON em.user_id=s.submitted_by AND em.org_id=s.org_id "
    "JOIN users u ON u.id=em.user_id "
)


_SUBMISSION_AUDIENCE = (
    "(:is_admin=1 OR s.submitted_by=:user_id OR EXISTS ("
    "SELECT 1 FROM education_cost_center_grants g WHERE g.org_id=s.org_id "
    "AND g.cost_center_id=s.cost_center_id AND g.user_id=:user_id "
    "AND g.role='manager' AND g.state='active' AND c.state='active'))"
)


def archive_audience_sql() -> str:
    """Filter archive projections before count, pagination and export."""
    audience = _SUBMISSION_AUDIENCE.replace(":is_admin", ":_arc_admin").replace(":user_id", ":_arc_user")
    return (
        "((a.education_submission_id IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM education_submissions raw WHERE raw.print_file_id=a.print_file_id OR raw.job_id=a.job_id)"
        " AND NOT EXISTS (SELECT 1 FROM education_upload_operations raw_o JOIN print_files raw_f"
        " ON raw_f.id=a.print_file_id WHERE raw_o.final_path=raw_f.stored_path AND raw_o.final_path IS NOT NULL))"
        " OR EXISTS (" + _BASE_SELECT
        + " JOIN print_files f ON f.id=s.print_file_id AND f.org_id=s.org_id"
        + " AND f.created_by=s.submitted_by"
        + " JOIN education_upload_operations o ON o.operation_id=s.operation_id"
        + " AND o.org_id=s.org_id AND o.user_id=s.submitted_by"
        + " AND o.final_path=f.stored_path AND o.state='committed'"
        + " JOIN models m ON m.id=s.model_id AND m.org_id=s.org_id"
        + " AND m.print_file_id=f.id AND j.model_id=m.id"
        + " WHERE s.id=a.education_submission_id AND s.org_id=a.org_id"
        + " AND s.org_id=:_arc_org AND s.job_id=a.job_id"
        + " AND s.print_file_id=a.print_file_id"
        + " AND NOT EXISTS (SELECT 1 FROM education_upload_operations other_o"
        + " WHERE other_o.final_path=f.stored_path AND other_o.final_path IS NOT NULL"
        + " AND other_o.operation_id<>s.operation_id) AND " + audience + "))"
    )
