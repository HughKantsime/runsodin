"""O.D.I.N. — Tag Management (global tag operations across archives)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role

router = APIRouter()


class TagRename(PydanticBaseModel):
    old: str
    new: str


@router.get("/tags", tags=["Archives"])
def list_tags(
    user=Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Return all unique tags across archives with usage counts."""
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
    user=Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Bulk-rename a tag across all archives. Admin only."""
    if not body.old.strip() or not body.new.strip():
        raise HTTPException(status_code=400, detail="Tag names cannot be empty")

    old_tag = body.old.strip()
    new_tag = body.new.strip()

    rows = db.execute(
        text("SELECT id, tags FROM print_archives WHERE tags IS NOT NULL AND tags != ''")
    ).fetchall()

    updated = 0
    for r in rows:
        tags = [t.strip() for t in r[1].split(",") if t.strip()]
        if old_tag in tags:
            tags = [new_tag if t == old_tag else t for t in tags]
            # Deduplicate after rename
            tags = list(dict.fromkeys(tags))
            db.execute(
                text("UPDATE print_archives SET tags = :tags WHERE id = :id"),
                {"tags": ", ".join(tags), "id": r[0]},
            )
            updated += 1

    db.commit()
    return {"updated": updated}


@router.delete("/tags/{tag}", tags=["Archives"])
def delete_tag(
    tag: str,
    user=Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Remove a tag from all archives. Admin only."""
    tag = tag.strip()
    if not tag:
        raise HTTPException(status_code=400, detail="Tag name cannot be empty")

    rows = db.execute(
        text("SELECT id, tags FROM print_archives WHERE tags IS NOT NULL AND tags != ''")
    ).fetchall()

    updated = 0
    for r in rows:
        tags = [t.strip() for t in r[1].split(",") if t.strip()]
        if tag in tags:
            tags = [t for t in tags if t != tag]
            db.execute(
                text("UPDATE print_archives SET tags = :tags WHERE id = :id"),
                {"tags": ", ".join(tags), "id": r[0]},
            )
            updated += 1

    db.commit()
    return {"updated": updated}
