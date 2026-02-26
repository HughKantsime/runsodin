"""
models_library/services.py — Cost calculation service

calculate_job_cost lives here so other modules (jobs) can import it
without crossing into modules/models_library/routes.py (a boundary violation).
"""

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


def calculate_job_cost(
    db: Session,
    model_id: int = None,
    filament_grams: float = 0,
    print_hours: float = 1.0,
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
            filament_grams = filament_grams or model.total_filament_grams or 0
            print_hours = print_hours or model.build_time_hours or 1.0
            material_type = model.default_filament_type.value if model.default_filament_type else "PLA"

    # Try to get per-material cost
    filament_entry = db.query(FilamentLibrary).filter(
        FilamentLibrary.material == material_type,
        FilamentLibrary.cost_per_gram.isnot(None)
    ).first()

    if filament_entry and filament_entry.cost_per_gram:
        cost_per_gram = filament_entry.cost_per_gram
    else:
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

    margin = model.markup_percent if model and model.markup_percent else config["default_margin"]
    suggested_price = subtotal * (1 + margin / 100)

    return (round(subtotal, 2), round(suggested_price, 2), margin)
