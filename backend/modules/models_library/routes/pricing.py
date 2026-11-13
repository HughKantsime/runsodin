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
    # `configured` tells the screens whether anyone has saved pricing: until then, costs and
    # prices come from generic defaults and are not shown as if they were the owner's numbers.
    if not config:
        return {**DEFAULT_PRICING_CONFIG, "configured": False}
    return {**config.value, "configured": True}


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
    # Optional, 0 when absent (a configuration saved before 2.3.0 prices exactly as it did).
    selling_fee_percent: Optional[float] = Field(default=None, ge=0, lt=100, allow_inf_nan=False)
    selling_fee_fixed: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    plate_handling_min: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    ui_mode: Optional[str] = None


    @field_validator("spool_cost", "spool_weight", "hourly_rate", "electricity_rate",
                     "printer_wattage", "printer_cost", "printer_lifespan", "packaging_cost",
                     "failure_rate", "monthly_rent", "parts_per_month", "post_processing_min",
                     "packing_min", "support_min", "default_margin", "other_costs",
                     "selling_fee_percent", "selling_fee_fixed", "plate_handling_min", mode="before")
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

    # Same shape as GET; `configured` is reported, never stored.
    return {**config.value, "configured": True}


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

    from modules.models_library.pricing_rule import price_item, all_finite
    try:
        priced = price_item(config, material_cost=material_cost, print_hours=print_hours,
                            units_per_plate=units_per_bed, markup_percent=model.markup_percent)
    except ValueError:
        raise HTTPException(status_code=422, detail="Selling fee percentage must be under 100") from None
    margin = priced["markup_percent"]
    if not all_finite(priced) or not all(math.isfinite(v) for v in (filament_grams, print_hours, cost_per_gram)):
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
            "material": round(priced["material"], 2),
            "labor": round(priced["labor"], 2),
            "electricity": round(priced["electricity"], 2),
            "depreciation": round(priced["depreciation"], 2),
            "packaging": round(priced["packaging"], 2),
            "failure": round(priced["failure"], 2),
            "overhead": round(priced["overhead"], 2),
            "other": round(priced["other"], 2)
        },
        "subtotal": round(priced["subtotal"], 2),
        # "margin_percent" has always held the markup (added on top of cost); kept for
        # compatibility. "markup_percent" is the same number under its right name.
        "margin_percent": margin,
        "markup_percent": margin,
        "selling_fees": round(priced["selling_fees"], 2),
        "margin_of_price_percent": round(priced["margin_of_price_percent"], 1),
        "suggested_price": round(priced["price"], 2)
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
    from core.controlled_parts import model_visibility
    visible, values = model_visibility(current_user)
    models = query.filter(text(visible)).params(**values).order_by(Model.name).all()

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

        # Calculate costs (one rule, modules/models_library/pricing_rule.py). This list has
        # always priced from the plate's grams and hours; only the per-plate handling minutes
        # are shared between the plate's units here.
        material_cost = filament_grams * cost_per_gram
        from modules.models_library.pricing_rule import price_item, all_finite
        margin = model.markup_percent if model.markup_percent else config["default_margin"]
        try:
            rule = price_item(config, material_cost=material_cost, print_hours=print_hours,
                              units_per_plate=model.units_per_bed or 1, markup_percent=margin)
            # One out-of-range input must never 500 the whole list: report that model's estimate as unknown.
            priced = all_finite(rule)
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            rule, priced = None, False
        subtotal = rule["subtotal"] if priced else None
        suggested_price = rule["price"] if priced else None

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
            # The markup actually used ("markup_percent" above is the model's own override, if any).
            "effective_markup_percent": margin if priced else None,
            "margin_of_price_percent": round(rule["margin_of_price_percent"], 1) if priced else None,
            "is_favorite": model.is_favorite or False,
            # Bed dimensions from linked print file (for dispatch compatibility warning)
            "bed_x_mm": bed_dims.get(model.id, {}).get("bed_x_mm"),
            "bed_y_mm": bed_dims.get(model.id, {}).get("bed_y_mm"),
        }
        result.append(model_dict)

    return result
