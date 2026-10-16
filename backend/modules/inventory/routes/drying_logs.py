"""Filament library CRUD endpoints."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from core.db import get_db
from core.rbac import require_role
from modules.inventory.models import FilamentLibrary
from ._helpers import FilamentCreateRequest, FilamentUpdateRequest

log = logging.getLogger("odin.api")
router = APIRouter(prefix="/filaments", tags=["Filaments"])


def _require_global_rate_writer(current_user: dict) -> None:
    """filament_library.cost_per_gram is one install-wide rate per filament, read by every
    org's cost estimates, so only a superadmin (admin with no org; on a single-tenant
    install every admin) may set or clear it. Same rule as core.rbac.require_superadmin."""
    if current_user.get("role") != "admin" or current_user.get("group_id"):
        raise HTTPException(status_code=403, detail="Superadmin access required to set filament cost per gram")


@router.get("", tags=["Filaments"])
def list_filaments(
    brand: Optional[str] = None,
    material: Optional[str] = None,
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db)
):
    """Get filaments from library."""
    query = db.query(FilamentLibrary)
    if brand:
        query = query.filter(FilamentLibrary.brand == brand)
    if material:
        query = query.filter(FilamentLibrary.material == material)

    library_filaments = query.all()
    result = []
    for f in library_filaments:
        result.append({
            "id": f"lib_{f.id}",
            "source": "library",
            "brand": f.brand,
            "name": f.name,
            "material": f.material,
            "color_hex": f.color_hex,
            "cost_per_gram": f.cost_per_gram,
            "display_name": f"{f.brand} {f.name} ({f.material})",
        })
    return result


@router.post("", tags=["Filaments"])
def add_custom_filament(data: FilamentCreateRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Add a custom filament to the library."""
    if data.cost_per_gram is not None:
        _require_global_rate_writer(current_user)
    filament = FilamentLibrary(
        brand=data.brand,
        name=data.name,
        material=data.material,
        color_hex=data.color_hex,
        cost_per_gram=data.cost_per_gram,
        is_custom=True,
    )
    db.add(filament)
    db.commit()
    return {"id": filament.id, "brand": filament.brand, "name": filament.name,
            "cost_per_gram": filament.cost_per_gram, "message": "Filament added"}


@router.get("/combined", tags=["Filaments"])
async def get_combined_filaments(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get filaments from both Spoolman (if available) and local library."""
    import httpx
    from core.config import settings

    result = []

    if settings.spoolman_url:
        try:
            from core.itar import pin_for_request, should_trust_env
            with pin_for_request(settings.spoolman_url):
                async with httpx.AsyncClient(trust_env=should_trust_env()) as client:
                    resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=5)
                    if resp.status_code == 200:
                        spools = resp.json()
                        for spool in spools:
                            filament = spool.get("filament", {})
                            result.append({
                                "id": f"spool_{spool['id']}",
                                "source": "spoolman",
                                "brand": filament.get("vendor", {}).get("name", "Unknown"),
                                "name": filament.get("name", "Unknown"),
                                "material": filament.get("material", "PLA"),
                                "color_hex": filament.get("color_hex"),
                                "remaining_weight": spool.get("remaining_weight"),
                                "display_name": f"{filament.get('name')} ({filament.get('material')}) - {int(spool.get('remaining_weight', 0))}g",
                            })
        except Exception as e:
            log.debug(f"Failed to fetch spoolman spools: {e}")

    library = db.query(FilamentLibrary).all()
    for f in library:
        result.append({
            "id": f"lib_{f.id}",
            "source": "library",
            "brand": f.brand,
            "name": f.name,
            "material": f.material,
            "color_hex": f.color_hex,
            "cost_per_gram": f.cost_per_gram,
            "display_name": f"{f.brand} {f.name} ({f.material})",
        })

    return result


@router.get("/{filament_id}", tags=["Filaments"])
def get_filament(filament_id: str, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get a specific filament from the library."""
    fid_str = filament_id.replace("lib_", "")
    try:
        fid = int(fid_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filament ID")

    filament = db.query(FilamentLibrary).filter(FilamentLibrary.id == fid).first()
    if not filament:
        raise HTTPException(status_code=404, detail="Filament not found")
    return {
        "id": f"lib_{filament.id}",
        "source": "library",
        "brand": filament.brand,
        "name": filament.name,
        "material": filament.material,
        "color_hex": filament.color_hex,
        "cost_per_gram": filament.cost_per_gram,
        "is_custom": getattr(filament, 'is_custom', False),
        "display_name": f"{filament.brand} {filament.name} ({filament.material})",
    }


@router.patch("/{filament_id}", tags=["Filaments"])
def update_filament(filament_id: str, updates: FilamentUpdateRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update a filament in the library."""
    fid_str = filament_id.replace("lib_", "")
    try:
        fid = int(fid_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filament ID")

    filament = db.query(FilamentLibrary).filter(FilamentLibrary.id == fid).first()
    if not filament:
        raise HTTPException(status_code=404, detail="Filament not found")

    update_data = updates.model_dump(exclude_unset=True)
    # material_cost_per_gram selects by (material, cost_per_gram, id); id is immutable, so
    # changing the rate, or moving a rated row to another material, needs a superadmin.
    if "cost_per_gram" in update_data and update_data["cost_per_gram"] != filament.cost_per_gram:
        _require_global_rate_writer(current_user)
    if ("material" in update_data and update_data["material"] != filament.material
            and filament.cost_per_gram is not None):
        _require_global_rate_writer(current_user)
    for field, value in update_data.items():
        setattr(filament, field, value)

    db.commit()
    return {
        "id": f"lib_{filament.id}",
        "brand": filament.brand,
        "name": filament.name,
        "material": filament.material,
        "color_hex": filament.color_hex,
        "cost_per_gram": filament.cost_per_gram,
        "message": "Filament updated",
    }


@router.delete("/{filament_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Filaments"])
def delete_filament(filament_id: str, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete a filament from the library."""
    fid_str = filament_id.replace("lib_", "")
    try:
        fid = int(fid_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filament ID")

    filament = db.query(FilamentLibrary).filter(FilamentLibrary.id == fid).first()
    if not filament:
        raise HTTPException(status_code=404, detail="Filament not found")
    if filament.cost_per_gram is not None:
        _require_global_rate_writer(current_user)  # removing a rated row moves the material's rate
    db.delete(filament)
    db.commit()
