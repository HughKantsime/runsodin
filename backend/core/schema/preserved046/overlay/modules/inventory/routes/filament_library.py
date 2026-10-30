"""Agent-surface filament library alias — GET /filament-library.

v1.9.0 Phase 2 (T3.9 + M3 resolution). The MCP `list_filaments` tool
calls `GET /api/v1/filament-library`. The existing backend exposes
`GET /api/v1/filaments` (drying_logs.py) returning library rows.

Rather than ship a breaking MCP update, this file adds a thin alias at
the path the MCP expects. It returns the same shape drying_logs.py
produces — same FilamentLibrary query, same dict structure — so agents
and portal get consistent data.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import (
    AGENT_READ_SCOPE,
    AGENT_WRITE_SCOPE,
    require_any_scope,
    require_role,
)
from modules.inventory.models import FilamentLibrary

log = logging.getLogger("odin.api")
router = APIRouter(prefix="/filament-library", tags=["Filaments"])


@router.get("", tags=["Filaments"])
def list_filament_library(
    brand: Optional[str] = None,
    material: Optional[str] = None,
    # Stacked auth (Phase 2 canonical read shape).
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(
        require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE)
    ),
    db: Session = Depends(get_db),
):
    """Get filament library entries.

    Agent-surface alias for the existing GET /api/v1/filaments route.
    Same response shape — mechanical alias for MCP list_filaments tool.
    """
    query = db.query(FilamentLibrary)
    if brand:
        query = query.filter(FilamentLibrary.brand == brand)
    if material:
        query = query.filter(FilamentLibrary.material == material)

    return [
        {
            "id": f"lib_{f.id}",
            "source": "library",
            "brand": f.brand,
            "name": f.name,
            "material": f.material,
            "color_hex": f.color_hex,
            "display_name": f"{f.brand} {f.name} ({f.material})",
        }
        for f in query.all()
    ]
