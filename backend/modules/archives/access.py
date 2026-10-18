"""Archive content ownership, independent of current printer visibility."""
from fastapi import HTTPException
from sqlalchemy import text
from core.rbac import get_org_scope, should_filter_org
from core.registry import registry
from core.tenancy_mode import NO_ORGANIZATIONS_SQL
from core.interfaces.education_policy import generic_archive_audience_sql


def archive_visibility(principal):
    org = get_org_scope(principal)
    if not should_filter_org(principal, org):
        return "1=1", {}
    provider = registry.get_provider("EducationPolicyProvider")
    audience = getattr(provider, "archive_audience_sql", None)
    # Older or absent authorities cannot authorize Education-marked history.
    education = audience() if callable(audience) else generic_archive_audience_sql()
    # Unresolved ownership (e.g. a print started at the printer, with no ODIN
    # job) resolves to the install while the install has no organizations;
    # with organizations it stays platform-admin only (fail-closed).
    return ("(((a.ownership_state='known' AND (a.org_id=:_arc_org OR a.org_id IS NULL)) "
            "OR (a.ownership_state='unresolved' AND a.org_id IS NULL AND " + NO_ORGANIZATIONS_SQL + ")) "
            "AND (" + education + "))", {
                "_arc_org": org, "_arc_user": principal.get("id"),
                "_arc_admin": 1 if principal.get("role") == "admin" else 0,
            })


def assert_archive_access(db, archive_id, principal):
    condition, parameters = archive_visibility(principal)
    found = db.execute(text("SELECT a.id FROM print_archives a WHERE a.id=:_arc_id AND " + condition),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- predicate is code-built from constant SQL fragments; principal values are bound
                       {**parameters, "_arc_id": archive_id}).first()
    if found is None:
        raise HTTPException(status_code=404, detail="Archive not found")
