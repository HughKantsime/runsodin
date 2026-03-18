#!/usr/bin/env python3
"""
marketing/seed.py — Populate ODIN staging with photogenic demo data via REST API.

Idempotent: safe to run multiple times. Checks for existing data before creating.

Environment variables:
    ODIN_BASE_URL       — default http://localhost:8000
    ODIN_ADMIN_USER     — default admin
    ODIN_ADMIN_PASSWORD — required
"""

import json
import logging
import os
import sys

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("odin.seed")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("ODIN_BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_USER = os.environ.get("ODIN_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ODIN_ADMIN_PASSWORD", "")

if not ADMIN_PASSWORD:
    log.error("ODIN_ADMIN_PASSWORD environment variable is required")
    sys.exit(1)

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()


def login():
    """Authenticate and store Bearer token in session headers."""
    url = f"{BASE_URL}/api/auth/login"
    resp = SESSION.post(url, data={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    resp.raise_for_status()
    token = resp.json()["access_token"]
    SESSION.headers.update({"Authorization": f"Bearer {token}"})
    log.info("Authenticated as %s", ADMIN_USER)


def api_get(path):
    """GET helper — returns parsed JSON."""
    resp = SESSION.get(f"{BASE_URL}/api{path}")
    resp.raise_for_status()
    return resp.json()


def api_post(path, payload):
    """POST helper — returns parsed JSON."""
    resp = SESSION.post(f"{BASE_URL}/api{path}", json=payload)
    resp.raise_for_status()
    return resp.json()


def api_patch(path, payload):
    """PATCH helper — returns parsed JSON."""
    resp = SESSION.patch(f"{BASE_URL}/api{path}", json=payload)
    resp.raise_for_status()
    return resp.json()


def find_by_name(items, name):
    """Find an item in a list by its 'name' field (case-insensitive)."""
    for item in items:
        if item.get("name", "").lower() == name.lower():
            return item
    return None


# ---------------------------------------------------------------------------
# Seed data definitions
# ---------------------------------------------------------------------------

FILAMENTS = [
    {"brand": "Bambu Lab", "name": "PLA Basic – Jade White", "material": "PLA", "color_hex": "F5F5F0", "cost_per_gram": 0.022},
    {"brand": "Bambu Lab", "name": "PLA Basic – Black", "material": "PLA", "color_hex": "1A1A1A", "cost_per_gram": 0.022},
    {"brand": "Bambu Lab", "name": "PLA Matte – Charcoal", "material": "PLA", "color_hex": "3B3B3B", "cost_per_gram": 0.028},
    {"brand": "Polymaker", "name": "PolyTerra PLA – Sakura Pink", "material": "PLA", "color_hex": "F4A7BB", "cost_per_gram": 0.024},
    {"brand": "Polymaker", "name": "PolyTerra PLA – Fossil Grey", "material": "PLA", "color_hex": "8E8E8E", "cost_per_gram": 0.024},
    {"brand": "Polymaker", "name": "PolyLite PETG – Translucent Blue", "material": "PETG", "color_hex": "4A90D9", "cost_per_gram": 0.030},
    {"brand": "Hatchbox", "name": "PETG – True Orange", "material": "PETG", "color_hex": "FF6B1A", "cost_per_gram": 0.026},
    {"brand": "Hatchbox", "name": "ABS – Signal Red", "material": "ABS", "color_hex": "CC2222", "cost_per_gram": 0.020},
    {"brand": "Prusament", "name": "PLA – Galaxy Silver", "material": "PLA", "color_hex": "B0B0C0", "cost_per_gram": 0.032},
    {"brand": "Prusament", "name": "PETG – Prusa Orange", "material": "PETG", "color_hex": "FA6831", "cost_per_gram": 0.035},
    {"brand": "Polymaker", "name": "PolyFlex TPU95 – White", "material": "TPU", "color_hex": "FAFAFA", "cost_per_gram": 0.042},
    {"brand": "Bambu Lab", "name": "PLA-CF – Matte Black", "material": "PLA", "color_hex": "222222", "cost_per_gram": 0.048},
]

PRINTERS = [
    {"name": "Alpha", "model": "Bambu Lab X1C", "api_type": "bambu", "slot_count": 4, "is_active": True, "bed_x_mm": 256, "bed_y_mm": 256},
    {"name": "Bravo", "model": "Bambu Lab X1C", "api_type": "bambu", "slot_count": 4, "is_active": True, "bed_x_mm": 256, "bed_y_mm": 256},
    {"name": "Charlie", "model": "Bambu Lab P1S", "api_type": "bambu", "slot_count": 4, "is_active": True, "bed_x_mm": 256, "bed_y_mm": 256},
    {"name": "Delta", "model": "Bambu Lab A1", "api_type": "bambu", "slot_count": 4, "is_active": True, "bed_x_mm": 256, "bed_y_mm": 256},
    {"name": "Echo", "model": "Prusa MK4", "api_type": "prusalink", "slot_count": 1, "is_active": True, "bed_x_mm": 250, "bed_y_mm": 210},
    {"name": "Foxtrot", "model": "Voron 2.4 350mm", "api_type": "moonraker", "slot_count": 1, "is_active": True, "bed_x_mm": 350, "bed_y_mm": 350},
    {"name": "Golf", "model": "Creality Ender-3 S1 Pro", "api_type": "moonraker", "slot_count": 1, "is_active": False, "bed_x_mm": 220, "bed_y_mm": 220},
    {"name": "Hotel", "model": "Bambu Lab P1S", "api_type": "bambu", "slot_count": 4, "is_active": True, "bed_x_mm": 256, "bed_y_mm": 256},
]

MODELS = [
    {"name": "Articulated Dragon", "build_time_hours": 3.5, "default_filament_type": "PLA", "category": "Toys", "cost_per_item": 1.20, "markup_percent": 350, "units_per_bed": 2, "notes": "Print-in-place, no supports needed"},
    {"name": "Desk Cable Organizer", "build_time_hours": 1.2, "default_filament_type": "PETG", "category": "Home & Office", "cost_per_item": 0.45, "markup_percent": 400, "units_per_bed": 6, "notes": "3-slot design, snap-fit assembly"},
    {"name": "Geometric Planter", "build_time_hours": 4.0, "default_filament_type": "PLA", "category": "Home & Garden", "cost_per_item": 2.10, "markup_percent": 300, "units_per_bed": 1, "notes": "Vase mode, 0.6mm nozzle recommended"},
    {"name": "Phone Stand – Universal", "build_time_hours": 0.8, "default_filament_type": "PLA", "category": "Accessories", "cost_per_item": 0.30, "markup_percent": 500, "units_per_bed": 8, "notes": "Fits phones up to 6.7 inches"},
    {"name": "Lithophane Frame 4x6", "build_time_hours": 6.0, "default_filament_type": "PLA", "category": "Custom", "cost_per_item": 1.80, "markup_percent": 450, "units_per_bed": 1, "notes": "100% infill, 0.12mm layer height"},
    {"name": "Flexi Rex", "build_time_hours": 1.5, "default_filament_type": "TPU", "category": "Toys", "cost_per_item": 0.90, "markup_percent": 350, "units_per_bed": 4, "notes": "Print-in-place, flexible joints"},
    {"name": "Headphone Hook – Desk Mount", "build_time_hours": 2.0, "default_filament_type": "PETG", "category": "Accessories", "cost_per_item": 0.65, "markup_percent": 400, "units_per_bed": 3, "notes": "Clamp-on design, 40mm max desk thickness"},
    {"name": "Miniature Terrain Set", "build_time_hours": 8.0, "default_filament_type": "PLA", "category": "Tabletop Gaming", "cost_per_item": 3.50, "markup_percent": 250, "units_per_bed": 1, "notes": "Ruins + scatter terrain, 0.2mm layer"},
]

SPOOLS = [
    {"filament_name": "PLA Basic – Jade White", "initial_weight_g": 1000, "spool_weight_g": 250, "vendor": "Bambu Lab", "storage_location": "Shelf A-1", "notes": "Primary white", "remaining_pct": 0.85},
    {"filament_name": "PLA Basic – Black", "initial_weight_g": 1000, "spool_weight_g": 250, "vendor": "Bambu Lab", "storage_location": "Shelf A-1", "notes": "Primary black", "remaining_pct": 0.72},
    {"filament_name": "PLA Matte – Charcoal", "initial_weight_g": 1000, "spool_weight_g": 250, "vendor": "Bambu Lab", "storage_location": "Shelf A-2", "notes": None, "remaining_pct": 0.60},
    {"filament_name": "PolyTerra PLA – Sakura Pink", "initial_weight_g": 1000, "spool_weight_g": 250, "vendor": "Polymaker", "storage_location": "Dry Box 1", "notes": "Popular color", "remaining_pct": 0.45},
    {"filament_name": "PolyTerra PLA – Fossil Grey", "initial_weight_g": 1000, "spool_weight_g": 250, "vendor": "Polymaker", "storage_location": "Dry Box 1", "notes": None, "remaining_pct": 0.90},
    {"filament_name": "PolyLite PETG – Translucent Blue", "initial_weight_g": 1000, "spool_weight_g": 250, "vendor": "Polymaker", "storage_location": "Dry Box 2", "notes": "Keep dry — hygroscopic", "remaining_pct": 0.55},
    {"filament_name": "PETG – True Orange", "initial_weight_g": 1000, "spool_weight_g": 250, "vendor": "Hatchbox", "storage_location": "Shelf B-1", "notes": None, "remaining_pct": 0.38},
    {"filament_name": "ABS – Signal Red", "initial_weight_g": 1000, "spool_weight_g": 250, "vendor": "Hatchbox", "storage_location": "Shelf B-2", "notes": "Enclosure required", "remaining_pct": 0.20},
    {"filament_name": "PLA – Galaxy Silver", "initial_weight_g": 1000, "spool_weight_g": 250, "vendor": "Prusament", "storage_location": "Shelf A-3", "notes": "Glitter finish", "remaining_pct": 0.95},
    {"filament_name": "PETG – Prusa Orange", "initial_weight_g": 1000, "spool_weight_g": 250, "vendor": "Prusament", "storage_location": "Dry Box 2", "notes": None, "remaining_pct": 0.65},
    {"filament_name": "PolyFlex TPU95 – White", "initial_weight_g": 750, "spool_weight_g": 250, "vendor": "Polymaker", "storage_location": "Dry Box 3", "notes": "Direct drive only", "remaining_pct": 0.50},
    {"filament_name": "PLA-CF – Matte Black", "initial_weight_g": 750, "spool_weight_g": 250, "vendor": "Bambu Lab", "storage_location": "Shelf A-2", "notes": "Hardened nozzle required", "remaining_pct": 0.15},
]

PRODUCTS = [
    {"name": "Articulated Dragon – PLA", "sku": "DRAG-PLA-001", "price": 14.99, "description": "Print-in-place articulated dragon, available in multiple colors"},
    {"name": "Desk Cable Organizer – 3 Slot", "sku": "CABLE-ORG-003", "price": 7.99, "description": "Snap-fit cable management for your desk setup"},
    {"name": "Geometric Planter – Large", "sku": "PLANT-GEO-LG", "price": 18.99, "description": "Modern geometric planter, waterproof inner pot included"},
    {"name": "Flexi Rex – TPU", "sku": "FLEX-REX-001", "price": 9.99, "description": "Adorable flexible T-Rex, print-in-place design"},
    {"name": "Lithophane Photo Frame 4x6", "sku": "LITHO-4X6-01", "price": 24.99, "description": "Custom lithophane from your photo, LED backlight included"},
]

ORDERS = [
    {
        "order_number": "ETSY-2847391056",
        "platform": "Etsy",
        "customer_name": "Sarah Mitchell",
        "customer_email": "sarah.m@example.com",
        "revenue": 29.98,
        "platform_fees": 1.95,
        "payment_fees": 1.17,
        "shipping_cost": 4.50,
        "shipping_charged": 5.99,
        "notes": "Gift wrap requested",
        "items": [
            {"product_name": "Articulated Dragon – PLA", "quantity": 2, "unit_price": 14.99},
        ],
    },
    {
        "order_number": "ETSY-2847396822",
        "platform": "Etsy",
        "customer_name": "James Kowalski",
        "customer_email": "jkowalski@example.com",
        "revenue": 52.96,
        "platform_fees": 3.45,
        "payment_fees": 2.03,
        "shipping_cost": 6.80,
        "shipping_charged": 7.99,
        "notes": None,
        "items": [
            {"product_name": "Geometric Planter – Large", "quantity": 2, "unit_price": 18.99},
            {"product_name": "Desk Cable Organizer – 3 Slot", "quantity": 2, "unit_price": 7.99},
        ],
    },
    {
        "order_number": "WEB-00412",
        "platform": "Website",
        "customer_name": "Priya Sharma",
        "customer_email": "priya.s@example.com",
        "revenue": 24.99,
        "platform_fees": 0.00,
        "payment_fees": 0.99,
        "shipping_cost": 3.20,
        "shipping_charged": 4.99,
        "notes": "Custom photo provided via email",
        "items": [
            {"product_name": "Lithophane Photo Frame 4x6", "quantity": 1, "unit_price": 24.99},
        ],
    },
    {
        "order_number": "LOCAL-0087",
        "platform": "Local Pickup",
        "customer_name": "Mike Torres",
        "customer_email": "mike.t@example.com",
        "revenue": 19.98,
        "platform_fees": 0.00,
        "payment_fees": 0.00,
        "shipping_cost": 0.00,
        "shipping_charged": 0.00,
        "notes": "Paid cash — farmer's market booth",
        "items": [
            {"product_name": "Flexi Rex – TPU", "quantity": 2, "unit_price": 9.99},
        ],
    },
]

JOBS = [
    {"item_name": "Articulated Dragon (Jade White)", "model_name": "Articulated Dragon", "duration_hours": 3.5, "priority": 1, "filament_type": "PLA", "colors_required": "Jade White", "notes": "Order ETSY-2847391056 — gift wrap"},
    {"item_name": "Articulated Dragon (Sakura Pink)", "model_name": "Articulated Dragon", "duration_hours": 3.5, "priority": 1, "filament_type": "PLA", "colors_required": "Sakura Pink", "notes": "Order ETSY-2847391056 — second unit"},
    {"item_name": "Geometric Planter x2", "model_name": "Geometric Planter", "duration_hours": 4.0, "priority": 2, "filament_type": "PLA", "colors_required": "Fossil Grey", "notes": "Order ETSY-2847396822"},
    {"item_name": "Cable Organizer Batch", "model_name": "Desk Cable Organizer", "duration_hours": 1.2, "priority": 3, "filament_type": "PETG", "colors_required": "Charcoal", "notes": "Order ETSY-2847396822 — 2 units on bed"},
    {"item_name": "Lithophane – Priya S.", "model_name": "Lithophane Frame 4x6", "duration_hours": 6.0, "priority": 2, "filament_type": "PLA", "colors_required": "Jade White", "notes": "Custom photo — check email for file"},
    {"item_name": "Flexi Rex Batch (White TPU)", "model_name": "Flexi Rex", "duration_hours": 1.5, "priority": 4, "filament_type": "TPU", "colors_required": "White", "notes": "Order LOCAL-0087 — farmer's market"},
    {"item_name": "Headphone Hooks – Stock Replenish", "model_name": "Headphone Hook – Desk Mount", "duration_hours": 2.0, "priority": 5, "filament_type": "PETG", "colors_required": "Translucent Blue", "notes": "Inventory replenishment — 3 units"},
    {"item_name": "Terrain Set – Customer Commission", "model_name": "Miniature Terrain Set", "duration_hours": 8.0, "priority": 3, "filament_type": "PLA", "colors_required": "Fossil Grey,Charcoal", "notes": "D&D commission — due end of week"},
]


# ---------------------------------------------------------------------------
# Seed functions
# ---------------------------------------------------------------------------


def seed_filaments():
    """Create filaments in the library, skipping any that already exist by name."""
    existing = api_get("/filaments")
    existing_names = {f["name"].lower() for f in existing}
    created = 0

    for fil in FILAMENTS:
        if fil["name"].lower() in existing_names:
            log.info("Filament already exists: %s — skipping", fil["name"])
            continue
        result = api_post("/filaments", fil)
        log.info("Created filament #%d: %s (%s)", result["id"], fil["name"], fil["material"])
        created += 1

    log.info("Filaments: %d created, %d skipped", created, len(FILAMENTS) - created)
    # Return fresh list for ID lookups
    return api_get("/filaments")


def seed_printers():
    """Create printers, skipping any that already exist by name."""
    existing = api_get("/printers")
    existing_names = {p["name"].lower() for p in existing}
    created = 0

    for printer in PRINTERS:
        if printer["name"].lower() in existing_names:
            log.info("Printer already exists: %s — skipping", printer["name"])
            continue
        result = api_post("/printers", printer)
        log.info("Created printer #%d: %s (%s)", result["id"], printer["name"], printer["model"])
        created += 1

    log.info("Printers: %d created, %d skipped", created, len(PRINTERS) - created)
    return api_get("/printers")


def seed_models():
    """Create models, skipping any that already exist by name."""
    existing = api_get("/models")
    existing_names = {m["name"].lower() for m in existing}
    created = 0

    for model in MODELS:
        if model["name"].lower() in existing_names:
            log.info("Model already exists: %s — skipping", model["name"])
            continue
        result = api_post("/models", model)
        log.info("Created model #%d: %s (%s)", result["id"], model["name"], model["category"])
        created += 1

    log.info("Models: %d created, %d skipped", created, len(MODELS) - created)
    return api_get("/models")


def seed_spools(filaments):
    """Create spools with varied fill levels. Skip if 10+ already exist."""
    existing = api_get("/spools")
    if len(existing) >= 10:
        log.info("Spools: %d already exist (>= 10) — skipping", len(existing))
        return existing

    # Build filament name -> id lookup
    fil_lookup = {}
    for f in filaments:
        fil_lookup[f["name"].lower()] = f["id"]

    created = 0
    for spool_def in SPOOLS:
        filament_name = spool_def["filament_name"]
        filament_id = fil_lookup.get(filament_name.lower())
        if filament_id is None:
            log.warning("Filament not found for spool: %s — skipping", filament_name)
            continue

        remaining_pct = spool_def["remaining_pct"]
        payload = {
            "filament_id": filament_id,
            "initial_weight_g": spool_def["initial_weight_g"],
            "spool_weight_g": spool_def["spool_weight_g"],
            "vendor": spool_def["vendor"],
            "storage_location": spool_def["storage_location"],
        }
        if spool_def["notes"]:
            payload["notes"] = spool_def["notes"]

        result = api_post("/spools", payload)
        spool_id = result["id"]

        # Set varied fill level via PATCH
        remaining_g = round(spool_def["initial_weight_g"] * remaining_pct, 1)
        api_patch(f"/spools/{spool_id}", {"remaining_weight_g": remaining_g})
        log.info(
            "Created spool #%d: %s (%.0f%% remaining)",
            spool_id, filament_name, remaining_pct * 100,
        )
        created += 1

    log.info("Spools: %d created", created)
    return api_get("/spools")


def seed_products():
    """Create products, skipping any that already exist by name."""
    existing = api_get("/products")
    existing_names = {p["name"].lower() for p in existing}
    created = 0

    for product in PRODUCTS:
        if product["name"].lower() in existing_names:
            log.info("Product already exists: %s — skipping", product["name"])
            continue
        result = api_post("/products", product)
        log.info("Created product #%d: %s ($%.2f)", result["id"], product["name"], product["price"])
        created += 1

    log.info("Products: %d created, %d skipped", created, len(PRODUCTS) - created)
    return api_get("/products")


def seed_orders(products):
    """Create orders with line items. Skip if 4+ already exist."""
    existing = api_get("/orders")
    if len(existing) >= 4:
        log.info("Orders: %d already exist (>= 4) — skipping", len(existing))
        return existing

    # Build product name -> id lookup
    prod_lookup = {}
    for p in products:
        prod_lookup[p["name"].lower()] = p["id"]

    created = 0
    for order_def in ORDERS:
        # Resolve product IDs for line items
        items = []
        for item in order_def["items"]:
            product_id = prod_lookup.get(item["product_name"].lower())
            if product_id is None:
                log.warning("Product not found for order item: %s — skipping item", item["product_name"])
                continue
            items.append({
                "product_id": product_id,
                "quantity": item["quantity"],
                "unit_price": item["unit_price"],
            })

        payload = {
            "order_number": order_def["order_number"],
            "platform": order_def["platform"],
            "customer_name": order_def["customer_name"],
            "customer_email": order_def["customer_email"],
            "revenue": order_def["revenue"],
            "platform_fees": order_def["platform_fees"],
            "payment_fees": order_def["payment_fees"],
            "shipping_cost": order_def["shipping_cost"],
            "shipping_charged": order_def["shipping_charged"],
            "items": items,
        }
        if order_def["notes"]:
            payload["notes"] = order_def["notes"]

        result = api_post("/orders", payload)
        log.info(
            "Created order #%d: %s (%s, $%.2f)",
            result["id"], order_def["order_number"], order_def["platform"], order_def["revenue"],
        )
        created += 1

    log.info("Orders: %d created", created)
    return api_get("/orders")


def seed_jobs(models_list):
    """Create jobs and advance the first two through lifecycle states. Skip if 6+ exist."""
    existing = api_get("/jobs")
    if len(existing) >= 6:
        log.info("Jobs: %d already exist (>= 6) — skipping", len(existing))
        return existing

    # Build model name -> id lookup
    model_lookup = {}
    for m in models_list:
        model_lookup[m["name"].lower()] = m["id"]

    created_ids = []
    for job_def in JOBS:
        model_id = model_lookup.get(job_def["model_name"].lower())
        if model_id is None:
            log.warning("Model not found for job: %s — skipping", job_def["model_name"])
            continue

        payload = {
            "item_name": job_def["item_name"],
            "model_id": model_id,
            "duration_hours": job_def["duration_hours"],
            "priority": job_def["priority"],
            "filament_type": job_def["filament_type"],
            "colors_required": job_def["colors_required"],
            "notes": job_def["notes"],
        }
        result = api_post("/jobs", payload)
        job_id = result["id"]
        created_ids.append(job_id)
        log.info("Created job #%d: %s (priority %d)", job_id, job_def["item_name"], job_def["priority"])

    # Advance first job through full lifecycle: queued -> in_progress -> completed
    if len(created_ids) >= 1:
        job_id = created_ids[0]
        try:
            api_post(f"/jobs/{job_id}/start", {})
            log.info("Job #%d started (in_progress)", job_id)
            api_post(f"/jobs/{job_id}/complete", {})
            log.info("Job #%d completed", job_id)
        except requests.HTTPError as e:
            log.warning("Could not advance job #%d through lifecycle: %s", job_id, e)

    # Start second job (leave in-progress)
    if len(created_ids) >= 2:
        job_id = created_ids[1]
        try:
            api_post(f"/jobs/{job_id}/start", {})
            log.info("Job #%d started (left in_progress)", job_id)
        except requests.HTTPError as e:
            log.warning("Could not start job #%d: %s", job_id, e)

    log.info("Jobs: %d created, 1 completed, 1 in-progress", len(created_ids))
    return api_get("/jobs")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    log.info("ODIN seed script starting — target: %s", BASE_URL)

    login()

    # Seed in dependency order
    filaments = seed_filaments()
    seed_printers()
    models_list = seed_models()
    seed_spools(filaments)
    products = seed_products()
    seed_orders(products)
    seed_jobs(models_list)

    log.info("Seed complete!")


if __name__ == "__main__":
    main()
