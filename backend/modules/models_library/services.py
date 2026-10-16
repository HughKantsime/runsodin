"""
models_library/services.py — Cost calculation service

calculate_job_cost lives here so other modules (jobs) can import it
without crossing into modules/models_library/routes.py (a boundary violation).
"""

import math

from fastapi import HTTPException
from sqlalchemy.orm import Session

from core.models import SystemConfig
from modules.models_library.models import Model
from modules.inventory.models import FilamentLibrary


DEFAULT_PRICING_CONFIG = {
    "spool_cost": 25.0,
    "spool_weight": 1000.0,
    "hourly_rate": 15.0,
    "electricity_rate": 0.12,
    "printer_wattage": 100,
    "printer_cost": 300.0,
    "printer_lifespan": 5000,
    "packaging_cost": 0.45,
    "failure_rate": 7.0,
    "monthly_rent": 0.0,
    "parts_per_month": 100,
    "post_processing_min": 5,
    "packing_min": 5,
    "support_min": 5,
    "default_margin": 50.0,
    "other_costs": 0.0,
    "ui_mode": "advanced"
}


def material_cost_per_gram(db: Session, material: str):
    """Per-material filament rate ($/g) from the filament library, or None.

    Models carry only a material type (no specific filament), so the rate is the
    lowest-id filament_library row of that material whose cost_per_gram is finite and
    non-negative. ORDER BY id makes the choice identical on SQLite and PostgreSQL, and a
    corrupt (non-finite/negative) row is skipped instead of poisoning every estimate.
    """
    rows = db.query(FilamentLibrary.cost_per_gram).filter(
        FilamentLibrary.material == material,
        FilamentLibrary.cost_per_gram.isnot(None),
    ).order_by(FilamentLibrary.id).all()
    for (value,) in rows:
        try:
            rate = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rate) and rate >= 0:
            return rate
    return None


def calculate_job_cost(
    db: Session,
    model_id: int = None,
    filament_grams: float = None,
    print_hours: float = None,
    material_type: str = "PLA",
):
    """Calculate estimated cost and suggested price for a job.

    Returns tuple: (estimated_cost, suggested_price, margin_percent)
    """
    # Get pricing config
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG

    # Get model for defaults if provided
    model = None
    if model_id:
        model = db.query(Model).filter(Model.id == model_id).first()
        if model:
            if filament_grams is None:
                filament_grams = model.total_filament_grams
            if print_hours is None:
                print_hours = model.build_time_hours
            material_type = model.default_filament_type.value if model.default_filament_type else "PLA"

    filament_grams = 0 if filament_grams is None else filament_grams
    print_hours = 1.0 if print_hours is None else print_hours

    # Try to get per-material cost
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

    margin = model.markup_percent if model and model.markup_percent is not None else config["default_margin"]
    suggested_price = subtotal * (1 + margin / 100)

    numeric_results = (
        filament_grams, print_hours, cost_per_gram, labor_hours, material_cost,
        labor_cost, electricity_cost, depreciation_cost, packaging_cost,
        base_cost, failure_cost, overhead_cost, subtotal, margin, suggested_price,
    )
    if not all(math.isfinite(value) for value in numeric_results):
        raise HTTPException(
            status_code=422,
            detail="Pricing calculation exceeds supported numeric range",
        )

    return (round(subtotal, 2), round(suggested_price, 2), margin)
