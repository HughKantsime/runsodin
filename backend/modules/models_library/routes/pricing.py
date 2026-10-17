"""Pricing configuration and model cost endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel as PydanticBaseModel, Field, ValidationError, field_validator
from sqlalchemy.orm import Session
import logging
import math

from core.db import get_db
from core.rbac import require_role, check_org_access, require_action
from core.models import SystemConfig
from modules.models_library.models import Model
from modules.inventory.models import FilamentLibrary
from modules.models_library.services import calculate_job_cost, DEFAULT_PRICING_CONFIG  # noqa: F401 — re-exported

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Pricing"])


@router.get("/pricing-config")
def get_pricing_config(current_user: dict = Depends(require_role("viewer", scope="read")), db: Session = Depends(get_db)):
    """Get system pricing configuration."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    if not config:
        # Return defaults if not configured
        return DEFAULT_PRICING_CONFIG
    return config.value


class PricingConfigRequest(PydanticBaseModel):
    spool_cost: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    spool_weight: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    hourly_rate: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    electricity_rate: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    printer_wattage: Optional[int] = Field(default=None, ge=0)
    printer_cost: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    printer_lifespan: Optional[int] = Field(default=None, gt=0)
    packaging_cost: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    failure_rate: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    monthly_rent: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    parts_per_month: Optional[int] = Field(default=None, ge=0)
    post_processing_min: Optional[int] = Field(default=None, ge=0)
    packing_min: Optional[int] = Field(default=None, ge=0)
    support_min: Optional[int] = Field(default=None, ge=0)
    default_margin: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    other_costs: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    ui_mode: Optional[str] = None


    @field_validator("spool_cost", "spool_weight", "hourly_rate", "electricity_rate",
                     "printer_wattage", "printer_cost", "printer_lifespan", "packaging_cost",
                     "failure_rate", "monthly_rent", "parts_per_month", "post_processing_min",
                     "packing_min", "support_min", "default_margin", "other_costs", mode="before")
    @classmethod
    def supplied_number(cls, value):
        if value is None or isinstance(value, bool):
            raise ValueError("A numeric value is required")
        return value


@router.put("/pricing-config")
def update_pricing_config(
    config_data: PricingConfigRequest,
    current_user: dict = Depends(require_action("settings.edit", scope="admin")),
    db: Session = Depends(get_db)
):
    """Update system pricing configuration."""

    config = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    existing = config.value if config else {}
    if not isinstance(existing, dict):
        raise HTTPException(status_code=409, detail="Stored pricing configuration must be an object")
    merged_config = {**DEFAULT_PRICING_CONFIG, **existing, **config_data.model_dump(exclude_unset=True)}
    try:
        validated = PricingConfigRequest.model_validate(merged_config)
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        raise HTTPException(status_code=422, detail={"invalid_fields": fields}) from None
    # Preserve unknown keys while normalizing supported numeric strings.
    merged_config.update(validated.model_dump(exclude_unset=True))
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
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer", scope="read:models")),
):
    """Calculate cost breakdown for a model using system pricing config."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model or not check_org_access(current_user, model.org_id):
        raise HTTPException(status_code=404, detail="Model not found")

    # Get pricing config
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG

    # Calculate costs per sellable item. Filament and print time are recorded per
    # bed, so material, electricity and machine depreciation divide by the bed's
    # unit count; labour, packaging, overhead and other costs are per item.
    units_per_bed = model.units_per_bed if model.units_per_bed and model.units_per_bed > 0 else 1
    bed_filament_grams = model.total_filament_grams or 0
    bed_print_hours = model.build_time_hours if model.build_time_hours is not None else 1.0
    filament_grams = bed_filament_grams / units_per_bed
    print_hours = bed_print_hours / units_per_bed

    # Try to get per-material cost from FilamentLibrary
    material_type = model.default_filament_type.value if model.default_filament_type else "PLA"
    from modules.models_library.services import material_cost_per_gram
    cost_per_gram = material_cost_per_gram(db, material_type)
    if cost_per_gram is not None:
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

    margin = model.markup_percent if model.markup_percent is not None else config["default_margin"]
    suggested_price = subtotal * (1 + margin / 100)

    numeric_results = (
        filament_grams, print_hours, cost_per_gram, material_cost, labor_cost,
        electricity_cost, depreciation_cost, packaging_cost, base_cost,
        failure_cost, overhead_cost, subtotal, margin, suggested_price,
    )
    if not all(math.isfinite(value) for value in numeric_results):
        raise HTTPException(status_code=422, detail="Pricing calculation exceeds supported numeric range")

    return {
        "model_id": model_id,
        "model_name": model.name,
        "units_per_bed": units_per_bed,
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
