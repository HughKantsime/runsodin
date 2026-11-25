"""O.D.I.N. — Tag Management (global tag operations across archives)."""

from core.rbac import should_filter_org, check_org_write, unscoped_write_detail

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import get_org_scope, require_role

from modules.archives.access import archive_visibility

router = APIRouter()


class TagRename(PydanticBaseModel):
    old: str
    new: str


@router.get("/tags", tags=["Archives"])
def list_tags(
    user=Depends(require_role("viewer", scope="read:jobs")),
    db: Session = Depends(get_db),
):
    """Return all unique tags across archives with usage counts."""
    org = get_org_scope(user)
    predicate, bindings = archive_visibility(user)
    if should_filter_org(user, org):
        rows = db.execute(
            text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- trusted-plugin SQL from EducationPolicyProvider.archive_audience_sql() (constant per its interface contract) plus module-constant fragments; principal values are bound
                "SELECT a.tags FROM print_archives a "
                "LEFT JOIN printers p ON a.printer_id = p.id "
                "WHERE a.tags IS NOT NULL AND a.tags != '' "
                f"AND {predicate}"
            ),
            bindings,
        ).fetchall()
    else:
        rows = db.execute(
            text("SELECT tags FROM print_archives WHERE tags IS NOT NULL AND tags != ''")
        ).fetchall()

    counts = {}
    for r in rows:
        for t in r[0].split(","):
            tag = t.strip()
            if tag:
                counts[tag] = counts.get(tag, 0) + 1

    return {"tags": [{"name": k, "count": v} for k, v in sorted(counts.items())]}


@router.post("/tags/rename", tags=["Archives"])
def rename_tag(
    body: TagRename,
    user=Depends(require_role("admin", scope="write:jobs")),
    db: Session = Depends(get_db),
):
    """Bulk-rename a tag across all archives. Admin only."""
    if not body.old.strip() or not body.new.strip():
        raise HTTPException(status_code=400, detail="Tag names cannot be empty")

    old_tag = body.old.strip()
    new_tag = body.new.strip()

    return _change_tag(db, user, old_tag, new_tag)


@router.delete("/tags/{tag}", tags=["Archives"])
def delete_tag(
    tag: str,
    user=Depends(require_role("admin", scope="write:jobs")),
    db: Session = Depends(get_db),
):
    """Remove a tag from all archives. Admin only."""
    tag = tag.strip()
    if not tag:
        raise HTTPException(status_code=400, detail="Tag name cannot be empty")

    return _change_tag(db, user, tag)


def _change_tag(db, user, old_tag, new_tag=None):
    predicate, bindings = "1=1", {}
    if should_filter_org(user, get_org_scope(user)):
        predicate, bindings = archive_visibility(user)
    rows = db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- archive visibility supplies the predicate and bound parameters
        "SELECT a.id, a.org_id, a.tags FROM print_archives a "
        f"WHERE a.tags IS NOT NULL AND a.tags != '' AND {predicate} ORDER BY a.id"
    ), bindings).all()
    updated = 0
    skipped = []
    for row in rows:
        tags = [t.strip() for t in row.tags.split(",") if t.strip()]
        if old_tag not in tags:
            continue
        if not check_org_write(user, row.org_id):
            skipped.append({"archive_id": row.id, "detail": unscoped_write_detail("archive")})
            continue
        if new_tag is None:
            tags = [t for t in tags if t != old_tag]
        else:
            tags = list(dict.fromkeys(new_tag if t == old_tag else t for t in tags))
        db.execute(text("UPDATE print_archives SET tags = :tags WHERE id = :id"),
                   {"tags": ", ".join(tags), "id": row.id})
        updated += 1
    db.commit()
    return {"updated": updated, **({"skipped": skipped} if skipped else {})}
