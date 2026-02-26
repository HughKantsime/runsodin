"""Pricing configuration and model cost endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import logging

from core.db import get_db
from core.rbac import require_role
from core.models import SystemConfig
from modules.models_library.models import Model
from modules.inventory.models import FilamentLibrary
from modules.models_library.services import calculate_job_cost, DEFAULT_PRICING_CONFIG  # noqa: F401 — re-exported

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Pricing"])


@router.get("/pricing-config")
def get_pricing_config(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get system pricing configuration."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    if not config:
        # Return defaults if not configured
        return DEFAULT_PRICING_CONFIG
    return config.value


@router.put("/pricing-config")
def update_pricing_config(
    config_data: dict,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Update system pricing configuration."""

    # Merge with defaults to ensure all fields exist
    merged_config = {**DEFAULT_PRICING_CONFIG, **config_data}

    config = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    if config:
        config.value = merged_config
    else:
        config = SystemConfig(key="pricing_config", value=merged_config)
        db.add(config)

    db.commit()
    db.refresh(config)

    return config.value


@router.get("/models/{model_id}/cost")
def calculate_model_cost(
    model_id: int,
    db: Session = Depends(get_db)
):
    """Calculate cost breakdown for a model using system pricing config."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    # Get pricing config
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG

    # Calculate costs
    filament_grams = model.total_filament_grams or 0
    print_hours = model.build_time_hours or 1.0

    # Try to get per-material cost from FilamentLibrary
    material_type = model.default_filament_type.value if model.default_filament_type else "PLA"
    filament_entry = db.query(FilamentLibrary).filter(
        FilamentLibrary.material == material_type,
        FilamentLibrary.cost_per_gram.isnot(None)
    ).first()

    if filament_entry and filament_entry.cost_per_gram:
        cost_per_gram = filament_entry.cost_per_gram
        cost_source = f"per-material ({material_type})"
    else:
        cost_per_gram = config["spool_cost"] / config["spool_weight"]
        cost_source = "global default"

    material_cost = filament_grams * cost_per_gram

    labor_hours = (config["post_processing_min"] + config["packing_min"] + config["support_min"]) / 60
    labor_cost = labor_hours * config["hourly_rate"]

    electricity_cost = (config["printer_wattage"] / 1000) * print_hours * config["electricity_rate"]

    depreciation_cost = (config["printer_cost"] / config["printer_lifespan"]) * print_hours

    packaging_cost = config["packaging_cost"]

    base_cost = material_cost + labor_cost + electricity_cost + depreciation_cost + packaging_cost + config["other_costs"]

    failure_cost = base_cost * (config["failure_rate"] / 100)

    overhead_cost = config["monthly_rent"] / config["parts_per_month"] if config["parts_per_month"] > 0 else 0

    subtotal = base_cost + failure_cost + overhead_cost

    margin = model.markup_percent if model.markup_percent else config["default_margin"]
    suggested_price = subtotal * (1 + margin / 100)

    return {
        "model_id": model_id,
        "model_name": model.name,
        "filament_grams": filament_grams,
        "print_hours": print_hours,
        "material_type": material_type,
        "cost_per_gram": round(cost_per_gram, 4),
        "cost_source": cost_source,
        "costs": {
            "material": round(material_cost, 2),
            "labor": round(labor_cost, 2),
            "electricity": round(electricity_cost, 2),
            "depreciation": round(depreciation_cost, 2),
            "packaging": round(packaging_cost, 2),
            "failure": round(failure_cost, 2),
            "overhead": round(overhead_cost, 2),
            "other": round(config["other_costs"], 2)
        },
        "subtotal": round(subtotal, 2),
        "margin_percent": margin,
        "suggested_price": round(suggested_price, 2)
    }
