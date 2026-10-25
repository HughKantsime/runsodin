"""Secret-free tenant Education readiness projections."""
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db_type


def readiness_for(db: Session, effective_org: int) -> dict:
    mode = db.execute(
        text("SELECT value FROM system_config WHERE key='education_mode'")
    ).fetchone()
    oidc = db.execute(text("SELECT * FROM oidc_config WHERE is_enabled IS TRUE LIMIT 1")).fetchone()
    oidc_map = oidc._mapping if oidc else {}
    provider = oidc_map.get("provider_type") or "microsoft"
    reasons = []
    if not oidc:
        reasons.append("Enable an SSO identity provider")
    if not oidc_map.get("client_id"):
        reasons.append("Client ID is missing")
    if not oidc_map.get("client_secret_encrypted"):
        reasons.append("Client secret is missing")
    oidc_ready = not reasons
    auto_create = bool(oidc_map.get("auto_create_users"))
    tenant_ready = not auto_create or oidc_map.get("default_group_id") == effective_org
    classroom = db.execute(
        text("SELECT * FROM classroom_connections WHERE org_id=:org_id"),
        {"org_id": effective_org},
    ).fetchone()
    from modules.organizations.classroom_service import connection_status

    counts = db.execute(
        text(
            "SELECT "
            "(SELECT COUNT(*) FROM education_cost_centers WHERE org_id=:org_id AND state='active') centers, "
            "(SELECT COUNT(*) FROM education_cost_center_grants WHERE org_id=:org_id AND state='active' AND role='student') students, "
            "(SELECT COUNT(*) FROM education_cost_center_grants WHERE org_id=:org_id AND state='active' AND role='manager') managers, "
            "(SELECT COUNT(*) FROM education_cost_center_printers WHERE org_id=:org_id AND state='active') printers"
        ),
        {"org_id": effective_org},
    ).one()
    complete_centers = db.execute(
        text("""
            SELECT COUNT(*) FROM education_cost_centers c
            WHERE c.org_id=:org_id AND c.state='active'
            AND EXISTS (
                SELECT 1 FROM education_cost_center_grants g JOIN users u ON u.id=g.user_id
                WHERE g.cost_center_id=c.id AND g.org_id=c.org_id AND g.state='active'
                AND g.role='student' AND u.group_id=c.org_id AND u.is_active IS TRUE
            )
            AND EXISTS (
                SELECT 1 FROM education_cost_center_grants g JOIN users u ON u.id=g.user_id
                WHERE g.cost_center_id=c.id AND g.org_id=c.org_id AND g.state='active'
                AND g.role='manager' AND u.group_id=c.org_id AND u.is_active IS TRUE
            )
            AND EXISTS (
                SELECT 1 FROM education_cost_center_printers e JOIN printers p ON p.id=e.printer_id
                WHERE e.cost_center_id=c.id AND e.org_id=c.org_id AND e.state='active'
                AND p.org_id=c.org_id AND p.is_active IS TRUE
                AND (p.shared IS NULL OR p.shared IS FALSE)
            )
        """), {"org_id": effective_org},
    ).scalar_one()
    from modules.organizations.education_storage import storage_readiness
    from modules.organizations.education_submission_service import UPLOAD_ROOT

    return {
        "education_license": True,
        "education_mode": bool(mode and mode.value == "true"),
        "oidc": {
            "ready": oidc_ready,
            "provider": provider,
            "enabled": bool(oidc_map.get("is_enabled")),
            "reasons": reasons,
            "auto_create": auto_create,
            "tenant_ready": tenant_ready,
            "tenant_reasons": [] if tenant_ready else ["Set the auto-create default organisation to this school"],
        },
        "classroom": connection_status(classroom),
        "pilot": {
            "complete_centers": int(complete_centers),
            "active_centers": int(counts.centers),
            "student_grants": int(counts.students),
            "manager_grants": int(counts.managers),
            "printer_entitlements": int(counts.printers),
        },
        "storage": storage_readiness(UPLOAD_ROOT),
        "backup": {
            "database_backend": get_db_type(),
            "verified_workflow_available": True,
        },
    }

