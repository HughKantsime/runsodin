"""Pricing configuration and model cost endpoints."""

from datetime import datetime
import json
from sqlalchemy import text, bindparam
from core.rbac import should_filter_org, _get_org_filter, get_org_scope
from modules.models_library.services import material_cost_per_gram
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


def list_models_with_pricing(*, category, org_id, db, current_user, variant_rows):
    """List all print models with calculated cost and suggested price."""
    from modules.models_library.education_retention import visible_models
    query = visible_models(db)
    if category:
        query = query.filter(Model.category == category)
    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if should_filter_org(current_user, effective_org):
        query = query.filter((Model.org_id == effective_org) | (Model.org_id == None))
    models = query.order_by(Model.name).all()

    # Job aggregates have their own resource permission, independent of models.
    from core.api_token_policy import token_scope_granted
    scopes = current_user.get("_token_scopes", [])
    stats_allowed = not (scopes or current_user.get("_auth_kind") == "user_api_token") or token_scope_granted(scopes, "read:jobs")
    statistics = {model.id: {"completed_job_count": 0, "actual_print_hours": 0.0,
                             "unknown_timing_job_count": 0} for model in models}
    if stats_allowed and models:
        # Persisted Education ownership remains private even without its provider.
        # Include both job payloads and every file linked to the model: legacy
        # Education jobs may have only one of these references.
        statement = """
            SELECT j.model_id,j.actual_start,j.actual_end FROM jobs j
            JOIN models m ON m.id=j.model_id
            WHERE j.status='completed' AND j.model_id IN :stats_model_ids
            AND NOT EXISTS (SELECT 1 FROM education_submissions s
                WHERE s.job_id=j.id OR s.model_id=m.id
                    OR EXISTS (SELECT 1 FROM jobs ej WHERE ej.id=s.job_id AND ej.model_id=m.id)
                    OR s.print_file_id=j.print_file_id OR s.print_file_id=m.print_file_id)
            AND NOT EXISTS (SELECT 1 FROM print_files f
                WHERE (f.id=j.print_file_id OR f.id=m.print_file_id OR f.job_id=j.id OR f.model_id=m.id)
                AND (EXISTS (SELECT 1 FROM education_submissions s
                        JOIN print_files ef ON ef.id=s.print_file_id
                        WHERE s.print_file_id=f.id OR (f.stored_path IS NOT NULL AND ef.stored_path=f.stored_path))
                    OR EXISTS (SELECT 1 FROM education_upload_operations o
                        WHERE f.stored_path IS NOT NULL AND
                            (o.final_path=f.stored_path OR o.staging_path=f.stored_path OR o.cleanup_path=f.stored_path))))
        """
        params = {"stats_model_ids": list(statistics)}
        if should_filter_org(current_user, effective_org):
            if effective_org is None:
                statement += " AND j.charged_to_org_id IS NULL"
            else:
                statement += " AND j.charged_to_org_id=:stats_org"
                params["stats_org"] = effective_org
        for row in db.execute(text(statement).bindparams(bindparam("stats_model_ids", expanding=True)), params):
            stats = statistics.get(row.model_id)
            if stats is None:
                continue  # Only models selected by the authorized library query.
            stats["completed_job_count"] += 1
            try:
                start = datetime.fromisoformat(row.actual_start) if isinstance(row.actual_start, str) else row.actual_start
                end = datetime.fromisoformat(row.actual_end) if isinstance(row.actual_end, str) else row.actual_end
                hours = (end - start).total_seconds() / 3600 if start and end else 0
            except (ValueError, TypeError):
                hours = 0
            if hours > 0:
                stats["actual_print_hours"] += hours
            else:
                stats["unknown_timing_job_count"] += 1

    # Get pricing config once
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG

    # Count and derive dimensions from the same authorized file snapshots.
    variant_counts = {}
    bed_dims = {}
    for model in models:
        variants = variant_rows(db, model.id, current_user)
        variant_counts[model.id] = len(variants)
        row = next((row for row in variants if row.bed_x_mm is not None), None)
        bed_dims[model.id] = {
            "bed_x_mm": row.bed_x_mm if row else None,
            "bed_y_mm": row.bed_y_mm if row else None,
        }

    result = []
    for model in models:
        # Calculate cost for this model
        filament_grams = model.total_filament_grams or 0
        print_hours = model.build_time_hours or 1.0

        # Try to get per-material cost
        material_type = model.default_filament_type.value if model.default_filament_type else "PLA"
        cost_per_gram = material_cost_per_gram(db, material_type)
        if cost_per_gram is None:
            cost_per_gram = config["spool_cost"] / config["spool_weight"]

        # Calculate costs
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
        # One out-of-range input must never 500 the whole list: report that model's estimate as unknown.
        priced = all(isinstance(v, (int, float)) and math.isfinite(v) for v in (subtotal, suggested_price, margin))

        # Build response
        model_dict = {
            "id": model.id,
            "name": model.name,
            "build_time_hours": model.build_time_hours,
            "default_filament_type": model.default_filament_type.value if model.default_filament_type else None,
            "color_requirements": model.color_requirements,
            "category": model.category,
            "thumbnail_url": model.thumbnail_url,
            "thumbnail_b64": model.thumbnail_b64,
            "notes": model.notes,
            "cost_per_item": model.cost_per_item,
            "units_per_bed": model.units_per_bed,
            "markup_percent": model.markup_percent,
            "created_at": model.created_at.isoformat() if model.created_at else None,
            "updated_at": model.updated_at.isoformat() if model.updated_at else None,
            "required_colors": model.required_colors,
            "total_filament_grams": model.total_filament_grams,
            "variant_count": variant_counts.get(model.id, 0),
            "job_statistics": statistics[model.id] if stats_allowed else None,
            # New pricing fields
            "estimated_cost": round(subtotal, 2) if priced else None,
            "suggested_price": round(suggested_price, 2) if priced else None,
            "margin_percent": margin if priced else None,
            "is_favorite": model.is_favorite or False,
            # Bed dimensions from linked print file (for dispatch compatibility warning)
            "bed_x_mm": bed_dims.get(model.id, {}).get("bed_x_mm"),
            "bed_y_mm": bed_dims.get(model.id, {}).get("bed_y_mm"),
        }
        result.append(model_dict)

    return result
