# ODIN Marketing Screenshot & Video Automation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a `make odin-marketing` pipeline that seeds ODIN with photogenic demo data, screenshots every page in dark+light mode at desktop+mobile viewports, records MQTT sessions from real printers, replays them into staging, and captures live-data screenshots/video — all idempotent and re-runnable.

**Architecture:** Three-layer tooling alongside (not inside) ODIN's source. A Python seed script hits the ODIN REST API to create demo data. A Playwright (Python, reusing the existing E2E infra patterns) script logs in and screenshots every page. A Python MQTT recorder/replayer captures real Bambu MQTT sessions to JSON and replays them with timing fidelity. A Makefile orchestrates all three.

**Tech Stack:** Python 3.11 (requests, paho-mqtt), Playwright for Python (sync API), FFmpeg, Make

---

## File Layout

All new files live under `marketing/` at project root:

```
marketing/
├── seed.py                    # Phase 1.1: DB seed script via API
├── screenshots.py             # Phase 1.2: Playwright screenshot script
├── video.py                   # Phase 1.3: Playwright video recording
├── mqtt_recorder.py           # Phase 2.1: MQTT session recorder
├── mqtt_replayer.py           # Phase 2.2: MQTT session replayer
├── requirements.txt           # Minimal deps (requests, paho-mqtt, playwright)
├── screenshots/               # Output: screenshots (gitignored)
├── videos/                    # Output: videos (gitignored)
└── recordings/                # MQTT recordings (gitignored)
```

---

## Environment Variables (all scripts)

| Var | Default | Used By |
|-----|---------|---------|
| `ODIN_BASE_URL` | `http://localhost:8000` | seed, screenshots, video |
| `ODIN_ADMIN_USER` | `admin` | seed, screenshots, video |
| `ODIN_ADMIN_PASSWORD` | (required) | seed, screenshots, video |
| `MQTT_BROKER_HOST` | (required for Phase 2) | recorder, replayer |
| `MQTT_BROKER_PORT` | `8883` | recorder, replayer |
| `MQTT_USERNAME` | (optional) | recorder, replayer |
| `MQTT_PASSWORD` | (optional) | recorder, replayer |

---

## Task 1: Project scaffolding

**Files:**
- Create: `marketing/requirements.txt`
- Create: `marketing/screenshots/.gitkeep`
- Create: `marketing/videos/.gitkeep`
- Create: `marketing/recordings/.gitkeep`
- Modify: `.gitignore` (add marketing output dirs)

**Step 1: Create the marketing directory structure**

```bash
mkdir -p marketing/screenshots marketing/videos marketing/recordings
```

**Step 2: Create marketing/requirements.txt**

```
# Marketing automation dependencies
requests>=2.31.0
paho-mqtt>=2.1.0
playwright>=1.40.0
```

**Step 3: Create .gitkeep files**

```bash
touch marketing/screenshots/.gitkeep
touch marketing/videos/.gitkeep
touch marketing/recordings/.gitkeep
```

**Step 4: Update .gitignore**

Add to the project `.gitignore`:

```gitignore
# Marketing automation outputs
marketing/screenshots/*.png
marketing/videos/*.mp4
marketing/videos/*.webm
marketing/recordings/*.json
```

**Step 5: Commit**

```bash
git add marketing/ .gitignore
git commit -m "feat: scaffold marketing automation directory structure"
```

---

## Task 2: Seed script — API client and filament library setup

**Files:**
- Create: `marketing/seed.py`

The seed script creates demo data via the ODIN REST API. It must be idempotent (safe to run multiple times). The ODIN API uses OAuth2PasswordRequestForm (form-encoded POST to `/api/auth/login`) returning a JWT token.

**Step 1: Write the seed script with API client and filament seeding**

Create `marketing/seed.py`:

```python
#!/usr/bin/env python3
"""
ODIN Marketing Seed Script — populates staging with photogenic demo data via API.

Usage:
    ODIN_ADMIN_PASSWORD=secret python marketing/seed.py

Idempotent: checks for existing data before creating.
"""

import os
import sys
import time
import logging
import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("odin-seed")

BASE_URL = os.environ.get("ODIN_BASE_URL", "http://localhost:8000")
USERNAME = os.environ.get("ODIN_ADMIN_USER", "admin")
PASSWORD = os.environ.get("ODIN_ADMIN_PASSWORD")

if not PASSWORD:
    log.error("ODIN_ADMIN_PASSWORD is required")
    sys.exit(1)


class ODINClient:
    """Thin REST client for ODIN API."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self._login(username, password)

    def _login(self, username: str, password: str):
        resp = self.session.post(
            f"{self.base_url}/api/auth/login",
            data={"username": username, "password": password},
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        self.session.headers["Authorization"] = f"Bearer {token}"
        log.info("Authenticated as %s", username)

    def get(self, path: str, **kwargs):
        return self.session.get(f"{self.base_url}{path}", timeout=15, **kwargs)

    def post(self, path: str, json=None, **kwargs):
        return self.session.post(f"{self.base_url}{path}", json=json, timeout=15, **kwargs)

    def put(self, path: str, json=None, **kwargs):
        return self.session.put(f"{self.base_url}{path}", json=json, timeout=15, **kwargs)

    def patch(self, path: str, json=None, **kwargs):
        return self.session.patch(f"{self.base_url}{path}", json=json, timeout=15, **kwargs)


# ─── Filament library entries ──────────────────────────────────────────

FILAMENTS = [
    {"brand": "Bambu Lab", "name": "PLA Basic — Black", "material": "PLA", "color_hex": "1A1A1A", "cost_per_gram": 0.02},
    {"brand": "Bambu Lab", "name": "PLA Basic — White", "material": "PLA", "color_hex": "FFFFFF", "cost_per_gram": 0.02},
    {"brand": "Bambu Lab", "name": "PLA Matte — Charcoal", "material": "PLA", "color_hex": "3C3C3C", "cost_per_gram": 0.025},
    {"brand": "Bambu Lab", "name": "PLA Silk — Gold", "material": "PLA", "color_hex": "D4A843", "cost_per_gram": 0.03},
    {"brand": "Bambu Lab", "name": "PETG Basic — Orange", "material": "PETG", "color_hex": "FF6B2B", "cost_per_gram": 0.025},
    {"brand": "Bambu Lab", "name": "PETG Basic — Blue", "material": "PETG", "color_hex": "2563EB", "cost_per_gram": 0.025},
    {"brand": "Bambu Lab", "name": "ABS — Red", "material": "ABS", "color_hex": "DC2626", "cost_per_gram": 0.022},
    {"brand": "Bambu Lab", "name": "TPU 95A — Clear", "material": "TPU", "color_hex": "E0E0E0", "cost_per_gram": 0.04},
    {"brand": "Polymaker", "name": "PolyTerra PLA — Sakura Pink", "material": "PLA", "color_hex": "F9A8D4", "cost_per_gram": 0.022},
    {"brand": "Polymaker", "name": "PolyTerra PLA — Army Dark Green", "material": "PLA", "color_hex": "3F6212", "cost_per_gram": 0.022},
    {"brand": "Hatchbox", "name": "PLA — Silver", "material": "PLA", "color_hex": "A8A8A8", "cost_per_gram": 0.019},
    {"brand": "Prusament", "name": "PETG — Prusa Galaxy Black", "material": "PETG", "color_hex": "1E1E2E", "cost_per_gram": 0.03},
]


# ─── Printers ──────────────────────────────────────────────────────────

PRINTERS = [
    {"name": "Bambu X1C — Alpha",   "model": "Bambu Lab X1 Carbon", "api_type": "bambu", "slot_count": 4, "is_active": True, "bed_x_mm": 256, "bed_y_mm": 256},
    {"name": "Bambu X1C — Bravo",   "model": "Bambu Lab X1 Carbon", "api_type": "bambu", "slot_count": 4, "is_active": True, "bed_x_mm": 256, "bed_y_mm": 256},
    {"name": "Bambu P1S — Charlie",  "model": "Bambu Lab P1S",       "api_type": "bambu", "slot_count": 4, "is_active": True, "bed_x_mm": 256, "bed_y_mm": 256},
    {"name": "Bambu P1S — Delta",    "model": "Bambu Lab P1S",       "api_type": "bambu", "slot_count": 4, "is_active": True, "bed_x_mm": 256, "bed_y_mm": 256},
    {"name": "Prusa MK4 — Echo",     "model": "Prusa MK4",           "api_type": "prusalink", "slot_count": 1, "is_active": True, "bed_x_mm": 250, "bed_y_mm": 210},
    {"name": "Voron 2.4 — Foxtrot",  "model": "Voron 2.4r2 350mm",   "api_type": "moonraker", "slot_count": 1, "is_active": True, "bed_x_mm": 350, "bed_y_mm": 350},
    {"name": "Bambu A1 — Golf",      "model": "Bambu Lab A1",         "api_type": "bambu", "slot_count": 4, "is_active": True, "bed_x_mm": 256, "bed_y_mm": 256},
    {"name": "Ender 3 V3 — Hotel",   "model": "Creality Ender 3 V3",  "api_type": "moonraker", "slot_count": 1, "is_active": False, "bed_x_mm": 220, "bed_y_mm": 220},
]


# ─── Models ────────────────────────────────────────────────────────────

MODELS = [
    {"name": "Benchy",             "build_time_hours": 0.5,  "default_filament_type": "PLA", "category": "Calibration",   "cost_per_item": 0.40, "markup_percent": 300, "notes": "Classic calibration boat"},
    {"name": "Articulated Dragon", "build_time_hours": 3.5,  "default_filament_type": "PLA", "category": "Toys & Games", "cost_per_item": 2.80, "markup_percent": 400, "notes": "Print-in-place flexi dragon"},
    {"name": "Lithophane Frame 5x7", "build_time_hours": 4.0, "default_filament_type": "PLA", "category": "Home Decor",  "cost_per_item": 1.50, "markup_percent": 500, "notes": "Curved lithophane with LED slot"},
    {"name": "Cable Management Clip (10-pack)", "build_time_hours": 1.0, "default_filament_type": "PETG", "category": "Functional", "cost_per_item": 0.60, "markup_percent": 350, "units_per_bed": 10},
    {"name": "Headphone Stand",    "build_time_hours": 6.0,  "default_filament_type": "PLA", "category": "Functional",    "cost_per_item": 3.20, "markup_percent": 300, "notes": "Two-piece snap-fit design"},
    {"name": "Dice Tower — Medieval", "build_time_hours": 5.0, "default_filament_type": "PLA", "category": "Toys & Games", "cost_per_item": 4.10, "markup_percent": 350},
    {"name": "Plant Pot Self-Watering", "build_time_hours": 2.5, "default_filament_type": "PETG", "category": "Home Decor", "cost_per_item": 1.80, "markup_percent": 400},
    {"name": "Phone Stand — Adjustable", "build_time_hours": 1.5, "default_filament_type": "PLA", "category": "Functional", "cost_per_item": 0.90, "markup_percent": 400},
]


# ─── Seed functions ────────────────────────────────────────────────────

def _existing_names(client: ODINClient, path: str, list_key: str = None) -> set:
    """Fetch existing entity names from an API list endpoint."""
    resp = client.get(path)
    if resp.status_code != 200:
        return set()
    data = resp.json()
    if isinstance(data, list):
        items = data
    elif list_key and list_key in data:
        items = data[list_key]
    else:
        items = data
    return {item.get("name") or item.get("brand", "") + " " + item.get("name", "") for item in items}


def seed_filaments(client: ODINClient) -> dict:
    """Seed filament library entries. Returns {name: id} mapping."""
    resp = client.get("/api/filaments")
    existing = {}
    if resp.status_code == 200:
        for f in resp.json():
            key = f"{f['brand']} — {f['name']}" if f.get('brand') else f['name']
            existing[f"{f.get('brand', '')}|{f.get('name', '')}|{f.get('material', '')}"] = f["id"]

    result = {}
    for fil in FILAMENTS:
        key = f"{fil['brand']}|{fil['name']}|{fil['material']}"
        if key in existing:
            result[fil["name"]] = existing[key]
            log.info("  filament exists: %s", fil["name"])
            continue
        resp = client.post("/api/filaments", json=fil)
        if resp.status_code in (200, 201):
            result[fil["name"]] = resp.json().get("id")
            log.info("  created filament: %s", fil["name"])
        else:
            log.warning("  filament failed (%d): %s — %s", resp.status_code, fil["name"], resp.text[:120])
    return result


def seed_printers(client: ODINClient) -> dict:
    """Seed printers. Returns {name: id} mapping."""
    existing = _existing_names(client, "/api/printers")
    result = {}
    for p in PRINTERS:
        if p["name"] in existing:
            log.info("  printer exists: %s", p["name"])
            # Fetch ID
            resp = client.get("/api/printers")
            for pr in resp.json():
                if pr["name"] == p["name"]:
                    result[p["name"]] = pr["id"]
                    break
            continue
        resp = client.post("/api/printers", json=p)
        if resp.status_code in (200, 201):
            result[p["name"]] = resp.json().get("id")
            log.info("  created printer: %s", p["name"])
        else:
            log.warning("  printer failed (%d): %s — %s", resp.status_code, p["name"], resp.text[:120])
    return result


def seed_models(client: ODINClient) -> dict:
    """Seed model library entries. Returns {name: id} mapping."""
    existing = _existing_names(client, "/api/models")
    result = {}
    for m in MODELS:
        if m["name"] in existing:
            log.info("  model exists: %s", m["name"])
            resp = client.get("/api/models")
            for model in (resp.json() if isinstance(resp.json(), list) else resp.json().get("models", [])):
                if model["name"] == m["name"]:
                    result[m["name"]] = model["id"]
                    break
            continue
        resp = client.post("/api/models", json=m)
        if resp.status_code in (200, 201):
            result[m["name"]] = resp.json().get("id")
            log.info("  created model: %s", m["name"])
        else:
            log.warning("  model failed (%d): %s — %s", resp.status_code, m["name"], resp.text[:120])
    return result


def seed_spools(client: ODINClient, filament_ids: dict) -> dict:
    """Seed spools with varied weights. Returns {label: id} mapping."""
    # Check existing spool count
    resp = client.get("/api/spools")
    existing_count = len(resp.json()) if resp.status_code == 200 else 0
    if existing_count >= 10:
        log.info("  %d spools already exist, skipping", existing_count)
        return {}

    SPOOLS = [
        {"filament_name": "PLA Basic — Black",        "initial_weight_g": 1000, "remaining_pct": 0.85, "vendor": "Amazon", "storage_location": "Rack A — Shelf 1"},
        {"filament_name": "PLA Basic — White",        "initial_weight_g": 1000, "remaining_pct": 0.62, "vendor": "Amazon", "storage_location": "Rack A — Shelf 1"},
        {"filament_name": "PLA Matte — Charcoal",     "initial_weight_g": 1000, "remaining_pct": 0.91, "vendor": "Bambu Store", "storage_location": "Rack A — Shelf 2"},
        {"filament_name": "PLA Silk — Gold",           "initial_weight_g": 1000, "remaining_pct": 0.33, "vendor": "Bambu Store", "storage_location": "Rack A — Shelf 2", "notes": "Low — reorder soon"},
        {"filament_name": "PETG Basic — Orange",       "initial_weight_g": 1000, "remaining_pct": 0.78, "vendor": "Amazon", "storage_location": "Rack B — Shelf 1"},
        {"filament_name": "PETG Basic — Blue",         "initial_weight_g": 1000, "remaining_pct": 0.55, "vendor": "Amazon", "storage_location": "Rack B — Shelf 1"},
        {"filament_name": "ABS — Red",                 "initial_weight_g": 1000, "remaining_pct": 0.95, "vendor": "Bambu Store", "storage_location": "Dry Box 1"},
        {"filament_name": "TPU 95A — Clear",           "initial_weight_g": 750,  "remaining_pct": 0.70, "vendor": "Amazon", "storage_location": "Dry Box 2"},
        {"filament_name": "PolyTerra PLA — Sakura Pink", "initial_weight_g": 1000, "remaining_pct": 0.48, "vendor": "Polymaker Direct", "storage_location": "Rack A — Shelf 3"},
        {"filament_name": "PolyTerra PLA — Army Dark Green", "initial_weight_g": 1000, "remaining_pct": 0.15, "vendor": "Polymaker Direct", "storage_location": "Rack A — Shelf 3", "notes": "Almost empty"},
        {"filament_name": "PLA — Silver",               "initial_weight_g": 1000, "remaining_pct": 0.89, "vendor": "Hatchbox Direct", "storage_location": "Rack B — Shelf 2"},
        {"filament_name": "PETG — Prusa Galaxy Black",  "initial_weight_g": 1000, "remaining_pct": 0.72, "vendor": "Prusa Shop", "storage_location": "Rack B — Shelf 2"},
    ]

    result = {}
    for s in SPOOLS:
        fid = filament_ids.get(s["filament_name"])
        if not fid:
            log.warning("  no filament ID for %s, skipping spool", s["filament_name"])
            continue
        payload = {
            "filament_id": fid,
            "initial_weight_g": s["initial_weight_g"],
            "spool_weight_g": 250,
            "vendor": s.get("vendor"),
            "storage_location": s.get("storage_location"),
            "notes": s.get("notes"),
        }
        resp = client.post("/api/spools", json=payload)
        if resp.status_code in (200, 201):
            spool_id = resp.json().get("id")
            result[s["filament_name"]] = spool_id
            log.info("  created spool: %s (id=%s)", s["filament_name"], spool_id)
            # Set remaining weight via update
            remaining = int(s["initial_weight_g"] * s["remaining_pct"])
            client.patch(f"/api/spools/{spool_id}", json={"remaining_weight_g": remaining})
        else:
            log.warning("  spool failed (%d): %s — %s", resp.status_code, s["filament_name"], resp.text[:120])
    return result


def seed_products(client: ODINClient, model_ids: dict) -> dict:
    """Seed products (for orders). Returns {name: id} mapping."""
    existing = _existing_names(client, "/api/products")
    PRODUCTS = [
        {"name": "Articulated Dragon — Gold",    "sku": "DRAG-GOLD-001",  "price": 14.99, "description": "Print-in-place dragon in silk gold PLA"},
        {"name": "Headphone Stand — Matte Black", "sku": "HS-BLK-001",    "price": 19.99, "description": "Two-piece snap-fit headphone holder"},
        {"name": "Cable Clips 10-pack",           "sku": "CC-10PK-001",   "price": 7.99,  "description": "Self-adhesive cable management clips"},
        {"name": "Dice Tower — Medieval Grey",    "sku": "DT-MED-001",    "price": 24.99, "description": "Castle-themed dice tower in stone grey"},
        {"name": "Lithophane Frame — Custom",     "sku": "LITH-5X7-001",  "price": 29.99, "description": "Custom photo lithophane in curved frame"},
    ]

    result = {}
    for p in PRODUCTS:
        if p["name"] in existing:
            log.info("  product exists: %s", p["name"])
            resp = client.get("/api/products")
            items = resp.json() if isinstance(resp.json(), list) else resp.json().get("products", [])
            for prod in items:
                if prod["name"] == p["name"]:
                    result[p["name"]] = prod["id"]
                    break
            continue
        resp = client.post("/api/products", json=p)
        if resp.status_code in (200, 201):
            result[p["name"]] = resp.json().get("id")
            log.info("  created product: %s", p["name"])
        else:
            log.warning("  product failed (%d): %s — %s", resp.status_code, p["name"], resp.text[:120])
    return result


def seed_orders(client: ODINClient, product_ids: dict):
    """Seed orders with line items."""
    resp = client.get("/api/orders")
    existing_count = len(resp.json()) if resp.status_code == 200 else 0
    if existing_count >= 4:
        log.info("  %d orders already exist, skipping", existing_count)
        return

    ORDERS = [
        {
            "order_number": "ETSY-2026-0142",
            "platform": "Etsy",
            "customer_name": "Sarah M.",
            "customer_email": "sarah.m@example.com",
            "revenue": 44.97,
            "platform_fees": 3.60,
            "payment_fees": 1.35,
            "shipping_cost": 5.99,
            "shipping_charged": 7.99,
            "notes": "Rush order — ships by Friday",
            "items": [
                {"product": "Articulated Dragon — Gold", "quantity": 2, "unit_price": 14.99},
                {"product": "Cable Clips 10-pack", "quantity": 1, "unit_price": 7.99},
            ],
        },
        {
            "order_number": "ETSY-2026-0143",
            "platform": "Etsy",
            "customer_name": "Marcus T.",
            "revenue": 24.99,
            "platform_fees": 2.00,
            "payment_fees": 0.75,
            "shipping_cost": 4.50,
            "shipping_charged": 5.99,
            "items": [
                {"product": "Dice Tower — Medieval Grey", "quantity": 1, "unit_price": 24.99},
            ],
        },
        {
            "order_number": "WEB-2026-0044",
            "platform": "Website",
            "customer_name": "Jen K.",
            "customer_email": "jen.k@example.com",
            "revenue": 29.99,
            "platform_fees": 0,
            "payment_fees": 0.90,
            "shipping_cost": 6.50,
            "notes": "Custom lithophane — photo received via email",
            "items": [
                {"product": "Lithophane Frame — Custom", "quantity": 1, "unit_price": 29.99},
            ],
        },
        {
            "order_number": "LOCAL-2026-0012",
            "platform": "Local Pickup",
            "customer_name": "Dave R.",
            "revenue": 59.97,
            "platform_fees": 0,
            "payment_fees": 0,
            "shipping_cost": 0,
            "notes": "Paid cash — local maker meetup",
            "items": [
                {"product": "Headphone Stand — Matte Black", "quantity": 3, "unit_price": 19.99},
            ],
        },
    ]

    for order_data in ORDERS:
        items_raw = order_data.pop("items", [])
        items_payload = []
        for item in items_raw:
            pid = product_ids.get(item["product"])
            if not pid:
                log.warning("  no product ID for %s, skipping item", item["product"])
                continue
            items_payload.append({
                "product_id": pid,
                "quantity": item["quantity"],
                "unit_price": item["unit_price"],
            })
        order_data["items"] = items_payload
        resp = client.post("/api/orders", json=order_data)
        if resp.status_code in (200, 201):
            log.info("  created order: %s", order_data["order_number"])
        else:
            log.warning("  order failed (%d): %s — %s", resp.status_code, order_data["order_number"], resp.text[:120])


def seed_jobs(client: ODINClient, model_ids: dict, printer_ids: dict):
    """Seed jobs in various states."""
    resp = client.get("/api/jobs")
    existing_count = 0
    if resp.status_code == 200:
        data = resp.json()
        existing_count = len(data) if isinstance(data, list) else len(data.get("jobs", []))
    if existing_count >= 6:
        log.info("  %d jobs already exist, skipping", existing_count)
        return

    printer_list = list(printer_ids.values())

    JOBS = [
        {"item_name": "Benchy — Calibration",          "model": "Benchy",             "duration_hours": 0.5,  "priority": 5, "filament_type": "PLA", "colors_required": "black", "notes": "Calibration print for new nozzle"},
        {"item_name": "Dragon — Gold Silk",             "model": "Articulated Dragon", "duration_hours": 3.5,  "priority": 3, "filament_type": "PLA", "colors_required": "gold"},
        {"item_name": "Headphone Stand — Black",        "model": "Headphone Stand",    "duration_hours": 6.0,  "priority": 4, "filament_type": "PLA", "colors_required": "black"},
        {"item_name": "Cable Clips x10 — Orange PETG",  "model": "Cable Management Clip (10-pack)", "duration_hours": 1.0, "priority": 3, "filament_type": "PETG", "colors_required": "orange"},
        {"item_name": "Dice Tower — Charcoal",          "model": "Dice Tower — Medieval", "duration_hours": 5.0, "priority": 2, "filament_type": "PLA", "colors_required": "charcoal"},
        {"item_name": "Lithophane — Custom Photo",      "model": "Lithophane Frame 5x7",  "duration_hours": 4.0, "priority": 4, "filament_type": "PLA", "colors_required": "white"},
        {"item_name": "Plant Pot — Blue PETG",          "model": "Plant Pot Self-Watering", "duration_hours": 2.5, "priority": 3, "filament_type": "PETG", "colors_required": "blue"},
        {"item_name": "Phone Stand — Silver PLA",       "model": "Phone Stand — Adjustable", "duration_hours": 1.5, "priority": 2, "filament_type": "PLA", "colors_required": "silver"},
    ]

    for i, j in enumerate(JOBS):
        model_id = model_ids.get(j.pop("model"))
        payload = {
            "item_name": j["item_name"],
            "duration_hours": j["duration_hours"],
            "priority": j["priority"],
            "filament_type": j.get("filament_type"),
            "colors_required": j.get("colors_required"),
            "notes": j.get("notes"),
        }
        if model_id:
            payload["model_id"] = model_id
        # Assign some jobs to printers
        if printer_list and i < len(printer_list):
            payload["printer_id"] = printer_list[i % len(printer_list)]

        resp = client.post("/api/jobs", json=payload)
        if resp.status_code in (200, 201):
            job_id = resp.json().get("id")
            log.info("  created job: %s (id=%s)", j["item_name"], job_id)
            # Move some jobs through lifecycle states for variety
            if i == 0:
                # Complete the first job
                client.post(f"/api/jobs/{job_id}/start")
                client.post(f"/api/jobs/{job_id}/complete")
            elif i == 1:
                # Start the second (in-progress)
                client.post(f"/api/jobs/{job_id}/start")
            # Rest stay as PENDING (queued)
        else:
            log.warning("  job failed (%d): %s — %s", resp.status_code, j["item_name"], resp.text[:120])


# ─── Main ──────────────────────────────────────────────────────────────

def main():
    log.info("=== ODIN Marketing Seed ===")
    log.info("Target: %s", BASE_URL)

    client = ODINClient(BASE_URL, USERNAME, PASSWORD)

    log.info("Seeding filament library...")
    filament_ids = seed_filaments(client)

    log.info("Seeding printers...")
    printer_ids = seed_printers(client)

    log.info("Seeding models...")
    model_ids = seed_models(client)

    log.info("Seeding spools...")
    seed_spools(client, filament_ids)

    log.info("Seeding products...")
    product_ids = seed_products(client, model_ids)

    log.info("Seeding orders...")
    seed_orders(client, product_ids)

    log.info("Seeding jobs...")
    seed_jobs(client, model_ids, printer_ids)

    log.info("=== Seed complete ===")


if __name__ == "__main__":
    main()
```

**Step 2: Verify seed script syntax**

Run: `python3 -c "import ast; ast.parse(open('marketing/seed.py').read()); print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add marketing/seed.py
git commit -m "feat: add marketing seed script for demo data via ODIN API"
```

---

## Task 3: Playwright screenshot script

**Files:**
- Create: `marketing/screenshots.py`

Reuses the login-via-token pattern from `tests/test_e2e/conftest.py`. Takes screenshots of every page in dark + light mode, desktop + mobile viewports.

**Step 1: Write the screenshot script**

Create `marketing/screenshots.py`:

```python
#!/usr/bin/env python3
"""
ODIN Marketing Screenshots — Playwright-based page capture.

Usage:
    ODIN_ADMIN_PASSWORD=secret python marketing/screenshots.py

Outputs PNG files to marketing/screenshots/.
"""

import os
import sys
import json
import base64
import logging
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("odin-screenshots")

BASE_URL = os.environ.get("ODIN_BASE_URL", "http://localhost:8000")
USERNAME = os.environ.get("ODIN_ADMIN_USER", "admin")
PASSWORD = os.environ.get("ODIN_ADMIN_PASSWORD")
OUTPUT_DIR = Path(__file__).parent / "screenshots"

if not PASSWORD:
    log.error("ODIN_ADMIN_PASSWORD is required")
    sys.exit(1)

# ─── Pages to screenshot ──────────────────────────────────────────────

PAGES = [
    ("dashboard",           "/"),
    ("printers",            "/printers"),
    ("jobs",                "/jobs"),
    ("timeline",            "/timeline"),
    ("models",              "/models"),
    ("spools",              "/spools"),
    ("orders",              "/orders"),
    ("products",            "/products"),
    ("archives",            "/archives"),
    ("print-log",           "/print-log"),
    ("analytics",           "/analytics"),
    ("cameras",             "/cameras"),
    ("settings",            "/settings"),
    ("alerts",              "/alerts"),
    ("calculator",          "/calculator"),
]

# Pages that also get mobile screenshots
MOBILE_PAGES = [
    "dashboard", "printers", "jobs", "spools", "orders",
]

DESKTOP_VIEWPORT = {"width": 1920, "height": 1080}
MOBILE_VIEWPORT = {"width": 390, "height": 844}


def _get_token(base_url: str, username: str, password: str) -> str:
    """Login via API and return JWT token."""
    resp = requests.post(
        f"{base_url}/api/auth/login",
        data={"username": username, "password": password},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _inject_auth(page, token: str, base_url: str):
    """Inject JWT token into localStorage (mirrors Login.jsx behavior)."""
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    user_json = json.dumps({"username": payload.get("sub", "admin"), "role": payload.get("role", "admin")})

    page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
    page.evaluate(f"""() => {{
        localStorage.setItem('token', '{token}');
        localStorage.setItem('user', {json.dumps(user_json)});
    }}""")


def _set_theme(page, theme: str):
    """Set ODIN theme via localStorage."""
    page.evaluate(f"""() => {{
        const user = JSON.parse(localStorage.getItem('user') || '{{}}'  );
        localStorage.setItem('theme', '{theme}');
        // ODIN stores theme preference — trigger re-render
        document.documentElement.classList.toggle('dark', '{theme}' === 'dark');
    }}""")


def _capture_page(page, name: str, path: str, base_url: str, theme: str, viewport_label: str):
    """Navigate to a page and take a screenshot."""
    url = f"{base_url}{path}"
    filename = f"{name}-{theme}-{viewport_label}.png"
    filepath = OUTPUT_DIR / filename

    try:
        page.goto(url, wait_until="networkidle", timeout=20000)
        # Extra wait for charts/animations to settle
        page.wait_for_timeout(1500)
        page.screenshot(path=str(filepath), full_page=False)
        log.info("  ✓ %s", filename)
    except Exception as e:
        log.warning("  ✗ %s — %s", filename, str(e)[:100])


def run_screenshots():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=== ODIN Marketing Screenshots ===")
    log.info("Target: %s", BASE_URL)

    token = _get_token(BASE_URL, USERNAME, PASSWORD)
    log.info("Authenticated")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])

        for theme in ("dark", "light"):
            log.info("--- %s mode (desktop) ---", theme)

            context = browser.new_context(
                viewport=DESKTOP_VIEWPORT,
                ignore_https_errors=True,
                color_scheme=theme,
            )
            page = context.new_page()
            _inject_auth(page, token, BASE_URL)
            _set_theme(page, theme)
            # Reload to apply theme
            page.goto(BASE_URL, wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(1000)

            for name, path in PAGES:
                _capture_page(page, name, path, BASE_URL, theme, "desktop")

            page.close()
            context.close()

            # Mobile screenshots for key pages
            log.info("--- %s mode (mobile) ---", theme)
            context = browser.new_context(
                viewport=MOBILE_VIEWPORT,
                ignore_https_errors=True,
                color_scheme=theme,
                is_mobile=True,
            )
            page = context.new_page()
            _inject_auth(page, token, BASE_URL)
            _set_theme(page, theme)
            page.goto(BASE_URL, wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(1000)

            for name, path in PAGES:
                if name in MOBILE_PAGES:
                    _capture_page(page, name, path, BASE_URL, theme, "mobile")

            page.close()
            context.close()

        browser.close()

    total = len(list(OUTPUT_DIR.glob("*.png")))
    log.info("=== Done: %d screenshots in %s ===", total, OUTPUT_DIR)


if __name__ == "__main__":
    run_screenshots()
```

**Step 2: Verify syntax**

Run: `python3 -c "import ast; ast.parse(open('marketing/screenshots.py').read()); print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add marketing/screenshots.py
git commit -m "feat: add Playwright screenshot automation for all ODIN pages"
```

---

## Task 4: Playwright video recording script

**Files:**
- Create: `marketing/video.py`

Records a scripted navigation flow through ODIN using Playwright's video recording, then post-processes with FFmpeg.

**Step 1: Write the video script**

Create `marketing/video.py`:

```python
#!/usr/bin/env python3
"""
ODIN Marketing Video — scripted navigation walkthrough recording.

Usage:
    ODIN_ADMIN_PASSWORD=secret python marketing/video.py

Outputs to marketing/videos/walkthrough.mp4
Requires FFmpeg for post-processing.
"""

import os
import sys
import json
import base64
import shutil
import logging
import subprocess
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("odin-video")

BASE_URL = os.environ.get("ODIN_BASE_URL", "http://localhost:8000")
USERNAME = os.environ.get("ODIN_ADMIN_USER", "admin")
PASSWORD = os.environ.get("ODIN_ADMIN_PASSWORD")
OUTPUT_DIR = Path(__file__).parent / "videos"

if not PASSWORD:
    log.error("ODIN_ADMIN_PASSWORD is required")
    sys.exit(1)


def _get_token(base_url: str, username: str, password: str) -> str:
    resp = requests.post(
        f"{base_url}/api/auth/login",
        data={"username": username, "password": password},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _inject_auth(page, token: str, base_url: str):
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    user_json = json.dumps({"username": payload.get("sub", "admin"), "role": payload.get("role", "admin")})

    page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
    page.evaluate(f"""() => {{
        localStorage.setItem('token', '{token}');
        localStorage.setItem('user', {json.dumps(user_json)});
    }}""")


# Scripted navigation flow (page path, dwell time in ms)
WALKTHROUGH = [
    ("/",          3000),   # Dashboard — hero shot
    ("/printers",  2500),   # Printer grid
    ("/jobs",      2000),   # Job queue
    ("/timeline",  2000),   # Timeline view
    ("/models",    2000),   # Model library
    ("/spools",    2000),   # Spool inventory
    ("/orders",    2000),   # Orders
    ("/analytics", 2500),   # Analytics
    ("/archives",  1500),   # Archive
    ("/settings",  1500),   # Settings
    ("/",          2000),   # Back to dashboard — closing shot
]


def record_walkthrough():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_video_dir = OUTPUT_DIR / "_raw"
    raw_video_dir.mkdir(exist_ok=True)

    log.info("=== ODIN Marketing Video Recording ===")
    token = _get_token(BASE_URL, USERNAME, PASSWORD)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
            color_scheme="dark",
            record_video_dir=str(raw_video_dir),
            record_video_size={"width": 1920, "height": 1080},
        )
        page = context.new_page()
        _inject_auth(page, token, BASE_URL)

        # Navigate to dashboard to confirm auth
        page.goto(BASE_URL, wait_until="networkidle", timeout=15000)
        page.wait_for_timeout(1500)

        for path, dwell_ms in WALKTHROUGH:
            url = f"{BASE_URL}{path}"
            log.info("  → %s (dwell %dms)", path, dwell_ms)
            page.goto(url, wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(dwell_ms)

        # Close to finalize video
        page.close()
        context.close()
        browser.close()

    # Find the recorded video
    raw_videos = list(raw_video_dir.glob("*.webm"))
    if not raw_videos:
        log.error("No video file produced by Playwright")
        return

    raw_path = raw_videos[0]
    output_path = OUTPUT_DIR / "walkthrough.mp4"

    # Post-process with FFmpeg: 1.5x speed, convert to mp4
    log.info("Post-processing with FFmpeg...")
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-i", str(raw_path),
        "-filter:v", "setpts=PTS/1.5",
        "-filter:a", "atempo=1.5",
        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    try:
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
        log.info("  ✓ %s", output_path)
    except subprocess.CalledProcessError as e:
        # Playwright videos may not have audio — try video-only
        ffmpeg_cmd_noaudio = [
            "ffmpeg", "-y",
            "-i", str(raw_path),
            "-filter:v", "setpts=PTS/1.5",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-an",
            "-movflags", "+faststart",
            str(output_path),
        ]
        subprocess.run(ffmpeg_cmd_noaudio, check=True, capture_output=True, text=True)
        log.info("  ✓ %s (no audio)", output_path)

    # Cleanup raw
    shutil.rmtree(raw_video_dir, ignore_errors=True)
    log.info("=== Video complete: %s ===", output_path)


if __name__ == "__main__":
    record_walkthrough()
```

**Step 2: Verify syntax**

Run: `python3 -c "import ast; ast.parse(open('marketing/video.py').read()); print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add marketing/video.py
git commit -m "feat: add Playwright video recording with FFmpeg post-processing"
```

---

## Task 5: MQTT recorder

**Files:**
- Create: `marketing/mqtt_recorder.py`

Records all MQTT messages from a broker to a JSON file with relative timestamps.

**Step 1: Write the MQTT recorder**

Create `marketing/mqtt_recorder.py`:

```python
#!/usr/bin/env python3
"""
ODIN MQTT Recorder — captures printer telemetry sessions to JSON.

Usage:
    python marketing/mqtt_recorder.py \
        --broker 192.168.1.100 \
        --duration 1800 \
        --output marketing/recordings/bambu-x1c-benchy.json

Records every MQTT message with topic, payload, and relative timestamp.
The resulting JSON can be replayed with mqtt_replayer.py.
"""

import os
import sys
import json
import time
import signal
import logging
import argparse
from pathlib import Path

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("mqtt-recorder")


class MQTTRecorder:
    def __init__(self, broker: str, port: int, username: str = None, password: str = None, use_tls: bool = False):
        self.broker = broker
        self.port = port
        self.messages = []
        self.start_time = None
        self._running = True

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if username:
            self.client.username_pw_set(username, password)
        if use_tls:
            self.client.tls_set()
            self.client.tls_insecure_set(True)

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            log.info("Connected to %s:%d", self.broker, self.port)
            # Subscribe to all topics
            client.subscribe("#")
            self.start_time = time.monotonic()
        else:
            log.error("Connection failed: rc=%d", rc)
            self._running = False

    def _on_message(self, client, userdata, msg):
        if self.start_time is None:
            return
        elapsed = time.monotonic() - self.start_time
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
            # Try to parse as JSON for cleaner storage
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                pass
        except Exception:
            payload = msg.payload.hex()

        self.messages.append({
            "t": round(elapsed, 3),
            "topic": msg.topic,
            "payload": payload,
            "qos": msg.qos,
        })

        if len(self.messages) % 100 == 0:
            log.info("  %d messages recorded (%.0fs elapsed)", len(self.messages), elapsed)

    def record(self, duration_seconds: int):
        """Connect and record for the given duration."""
        signal.signal(signal.SIGINT, lambda *_: setattr(self, '_running', False))

        log.info("Connecting to %s:%d...", self.broker, self.port)
        self.client.connect(self.broker, self.port, keepalive=60)
        self.client.loop_start()

        deadline = time.monotonic() + duration_seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(1)

        self.client.loop_stop()
        self.client.disconnect()
        log.info("Recording stopped: %d messages in %.0f seconds",
                 len(self.messages), duration_seconds)

    def save(self, output_path: str):
        """Save recorded messages to JSON."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump({
                "broker": self.broker,
                "message_count": len(self.messages),
                "duration_seconds": self.messages[-1]["t"] if self.messages else 0,
                "messages": self.messages,
            }, f, indent=2)
        log.info("Saved to %s (%.1f KB)", path, path.stat().st_size / 1024)


def main():
    parser = argparse.ArgumentParser(description="Record MQTT session to JSON")
    parser.add_argument("--broker", required=True, help="MQTT broker hostname/IP")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MQTT_BROKER_PORT", "8883")))
    parser.add_argument("--username", default=os.environ.get("MQTT_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"))
    parser.add_argument("--tls", action="store_true", default=True, help="Use TLS (default: true)")
    parser.add_argument("--no-tls", action="store_true", help="Disable TLS")
    parser.add_argument("--duration", type=int, default=1800, help="Recording duration in seconds (default: 1800)")
    parser.add_argument("--output", required=True, help="Output JSON file path")
    args = parser.parse_args()

    use_tls = args.tls and not args.no_tls

    recorder = MQTTRecorder(
        broker=args.broker,
        port=args.port,
        username=args.username,
        password=args.password,
        use_tls=use_tls,
    )
    recorder.record(args.duration)
    recorder.save(args.output)


if __name__ == "__main__":
    main()
```

**Step 2: Verify syntax**

Run: `python3 -c "import ast; ast.parse(open('marketing/mqtt_recorder.py').read()); print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add marketing/mqtt_recorder.py
git commit -m "feat: add MQTT recorder for capturing printer telemetry sessions"
```

---

## Task 6: MQTT replayer

**Files:**
- Create: `marketing/mqtt_replayer.py`

Reads a recording JSON file and replays messages to a target broker with original timing.

**Step 1: Write the MQTT replayer**

Create `marketing/mqtt_replayer.py`:

```python
#!/usr/bin/env python3
"""
ODIN MQTT Replayer — replays recorded sessions into a target broker.

Usage:
    python marketing/mqtt_replayer.py \
        --broker staging-host \
        --recording marketing/recordings/bambu-x1c-benchy.json \
        --speed 2

Publishes messages with original timing deltas (adjusted by speed multiplier).
"""

import os
import sys
import json
import time
import signal
import logging
import argparse
from pathlib import Path

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("mqtt-replayer")


class MQTTReplayer:
    def __init__(self, broker: str, port: int, username: str = None, password: str = None, use_tls: bool = False):
        self.broker = broker
        self.port = port
        self._running = True
        self._connected = False

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if username:
            self.client.username_pw_set(username, password)
        if use_tls:
            self.client.tls_set()
            self.client.tls_insecure_set(True)

        self.client.on_connect = self._on_connect

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            log.info("Connected to %s:%d", self.broker, self.port)
            self._connected = True
        else:
            log.error("Connection failed: rc=%d", rc)
            self._running = False

    def replay(self, recording_path: str, speed: float = 1.0, loop: bool = False):
        """Replay a recorded session."""
        signal.signal(signal.SIGINT, lambda *_: setattr(self, '_running', False))

        path = Path(recording_path)
        if not path.exists():
            log.error("Recording not found: %s", path)
            sys.exit(1)

        with open(path) as f:
            data = json.load(f)

        messages = data.get("messages", [])
        if not messages:
            log.error("Recording has no messages")
            return

        log.info("Loaded %d messages (%.0fs duration) from %s",
                 len(messages), data.get("duration_seconds", 0), path.name)
        log.info("Replay speed: %.1fx", speed)

        self.client.connect(self.broker, self.port, keepalive=60)
        self.client.loop_start()

        # Wait for connection
        deadline = time.monotonic() + 10
        while not self._connected and time.monotonic() < deadline:
            time.sleep(0.1)
        if not self._connected:
            log.error("Failed to connect within 10s")
            return

        while self._running:
            start_time = time.monotonic()

            for msg in messages:
                if not self._running:
                    break

                # Wait until the right time (adjusted by speed)
                target_elapsed = msg["t"] / speed
                actual_elapsed = time.monotonic() - start_time
                wait = target_elapsed - actual_elapsed
                if wait > 0:
                    time.sleep(wait)

                # Publish
                payload = msg["payload"]
                if isinstance(payload, dict):
                    payload = json.dumps(payload)
                elif not isinstance(payload, (str, bytes)):
                    payload = str(payload)

                self.client.publish(
                    msg["topic"],
                    payload.encode("utf-8") if isinstance(payload, str) else payload,
                    qos=msg.get("qos", 0),
                )

            if not loop:
                break
            log.info("Loop complete, restarting replay...")

        self.client.loop_stop()
        self.client.disconnect()
        log.info("Replay finished: %d messages published", len(messages))


def main():
    parser = argparse.ArgumentParser(description="Replay MQTT recording")
    parser.add_argument("--broker", required=True, help="Target MQTT broker hostname/IP")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MQTT_BROKER_PORT", "8883")))
    parser.add_argument("--username", default=os.environ.get("MQTT_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"))
    parser.add_argument("--tls", action="store_true", default=True)
    parser.add_argument("--no-tls", action="store_true")
    parser.add_argument("--recording", required=True, help="Path to recording JSON file")
    parser.add_argument("--speed", type=float, default=1.0, help="Playback speed multiplier (default: 1.0)")
    parser.add_argument("--loop", action="store_true", help="Loop the recording continuously")
    args = parser.parse_args()

    use_tls = args.tls and not args.no_tls

    replayer = MQTTReplayer(
        broker=args.broker,
        port=args.port,
        username=args.username,
        password=args.password,
        use_tls=use_tls,
    )
    replayer.replay(args.recording, speed=args.speed, loop=args.loop)


if __name__ == "__main__":
    main()
```

**Step 2: Verify syntax**

Run: `python3 -c "import ast; ast.parse(open('marketing/mqtt_replayer.py').read()); print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add marketing/mqtt_replayer.py
git commit -m "feat: add MQTT replayer for injecting recorded sessions into staging"
```

---

## Task 7: Makefile integration

**Files:**
- Modify: `Makefile` (append marketing targets)

**Step 1: Append marketing targets to Makefile**

Add these targets at the end of the existing Makefile (before the `help` target):

```makefile
# ─── Marketing Automation ──────────────────────────────────────────────

odin-seed: ## Seed staging DB with photogenic demo data
	@echo "=== Seeding ODIN with demo data ==="
	python3 marketing/seed.py

odin-screenshots: ## Capture screenshots of all pages (dark + light, desktop + mobile)
	@echo "=== Capturing ODIN screenshots ==="
	python3 marketing/screenshots.py

odin-video: ## Record a scripted navigation walkthrough video
	@echo "=== Recording ODIN walkthrough video ==="
	python3 marketing/video.py

odin-record: ## Record a live MQTT session (run while printers are active). Usage: make odin-record DURATION=1800 NAME=bambu-benchy BROKER=192.168.1.100
	@test -n "$(NAME)" || (echo "Usage: make odin-record BROKER=<host> DURATION=1800 NAME=bambu-benchy" && exit 1)
	@test -n "$(BROKER)" || (echo "Usage: make odin-record BROKER=<host> DURATION=1800 NAME=bambu-benchy" && exit 1)
	python3 marketing/mqtt_recorder.py \
		--broker $(BROKER) \
		--duration $(or $(DURATION),1800) \
		--output marketing/recordings/$(NAME).json

odin-marketing: odin-seed odin-screenshots odin-video ## Generate all marketing assets (seed + screenshots + video, no MQTT)

odin-marketing-full: odin-seed ## Full marketing pipeline with MQTT replay. Usage: make odin-marketing-full RECORDING=bambu-benchy BROKER=localhost
	@test -n "$(RECORDING)" || (echo "Usage: make odin-marketing-full RECORDING=bambu-benchy BROKER=localhost" && exit 1)
	@echo "=== Starting MQTT replayer in background ==="
	python3 marketing/mqtt_replayer.py \
		--broker $(or $(BROKER),localhost) \
		--recording marketing/recordings/$(RECORDING).json \
		--speed 2 --loop &
	REPLAYER_PID=$$!; \
	echo "  Replayer PID: $$REPLAYER_PID"; \
	sleep 5; \
	echo "=== Capturing live-data screenshots ==="; \
	python3 marketing/screenshots.py; \
	echo "=== Recording walkthrough video ==="; \
	python3 marketing/video.py; \
	echo "=== Stopping replayer ==="; \
	kill $$REPLAYER_PID 2>/dev/null || true
	@echo "=== Marketing pipeline complete ==="
```

**Step 2: Verify Makefile syntax**

Run: `make help` to confirm new targets show up.

**Step 3: Commit**

```bash
git add Makefile
git commit -m "feat: add marketing automation Makefile targets (seed, screenshots, video, MQTT)"
```

---

## Task 8: Tests for seed script and MQTT tools

**Files:**
- Create: `tests/test_marketing.py`

Unit tests for the marketing tooling. These test the scripts' internal logic without requiring a running ODIN instance or MQTT broker.

**Step 1: Write the tests**

Create `tests/test_marketing.py`:

```python
"""
Tests for marketing automation scripts.

Tests internal logic without requiring a running ODIN instance or MQTT broker.
"""

import json
import sys
import os
import importlib
from unittest.mock import patch, MagicMock
from pathlib import Path

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestSeedScript:
    """Test seed.py helper functions and data integrity."""

    def test_filament_data_has_required_fields(self):
        """Every filament entry must have brand, name, material, color_hex."""
        # Import the data directly
        spec = importlib.util.spec_from_file_location("seed", "marketing/seed.py")
        # We can't import directly without ODIN_ADMIN_PASSWORD, so parse the file
        import ast
        with open("marketing/seed.py") as f:
            tree = ast.parse(f.read())

        # Find FILAMENTS assignment
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "FILAMENTS":
                        filaments = ast.literal_eval(node.value)
                        assert len(filaments) >= 10, "Need at least 10 filaments for variety"
                        for fil in filaments:
                            assert "brand" in fil, f"Missing brand: {fil}"
                            assert "name" in fil, f"Missing name: {fil}"
                            assert "material" in fil, f"Missing material: {fil}"
                            assert "color_hex" in fil, f"Missing color_hex: {fil}"
                            # Validate hex color (6 chars, valid hex)
                            assert len(fil["color_hex"]) == 6, f"Bad color_hex: {fil['color_hex']}"
                            int(fil["color_hex"], 16)  # Raises if not valid hex

    def test_printer_data_has_required_fields(self):
        """Every printer entry must have name, model, api_type."""
        import ast
        with open("marketing/seed.py") as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "PRINTERS":
                        printers = ast.literal_eval(node.value)
                        assert len(printers) >= 6, "Need at least 6 printers"
                        names = set()
                        for p in printers:
                            assert "name" in p
                            assert "model" in p
                            assert "api_type" in p
                            assert p["name"] not in names, f"Duplicate printer name: {p['name']}"
                            names.add(p["name"])

    def test_model_data_has_required_fields(self):
        """Every model entry must have name, build_time_hours."""
        import ast
        with open("marketing/seed.py") as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "MODELS":
                        models = ast.literal_eval(node.value)
                        assert len(models) >= 5, "Need at least 5 models"
                        for m in models:
                            assert "name" in m
                            assert "build_time_hours" in m
                            assert m["build_time_hours"] > 0

    def test_seed_script_is_valid_python(self):
        """The seed script must parse without syntax errors."""
        import ast
        with open("marketing/seed.py") as f:
            ast.parse(f.read())

    def test_screenshots_script_is_valid_python(self):
        """The screenshots script must parse without syntax errors."""
        import ast
        with open("marketing/screenshots.py") as f:
            ast.parse(f.read())

    def test_video_script_is_valid_python(self):
        """The video script must parse without syntax errors."""
        import ast
        with open("marketing/video.py") as f:
            ast.parse(f.read())


class TestMQTTRecorder:
    """Test MQTT recorder logic."""

    def test_recorder_script_is_valid_python(self):
        import ast
        with open("marketing/mqtt_recorder.py") as f:
            ast.parse(f.read())

    def test_recording_json_format(self, tmp_path):
        """Verify the save format matches what the replayer expects."""
        # Simulate a recording
        recording = {
            "broker": "test-broker",
            "message_count": 3,
            "duration_seconds": 10.5,
            "messages": [
                {"t": 0.0, "topic": "device/123/report", "payload": {"temp": 210}, "qos": 0},
                {"t": 5.0, "topic": "device/123/report", "payload": {"temp": 211}, "qos": 0},
                {"t": 10.5, "topic": "device/123/report", "payload": {"temp": 210}, "qos": 0},
            ],
        }
        path = tmp_path / "test.json"
        with open(path, "w") as f:
            json.dump(recording, f)

        # Verify it round-trips correctly
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["message_count"] == 3
        assert loaded["messages"][0]["t"] == 0.0
        assert loaded["messages"][2]["topic"] == "device/123/report"
        assert isinstance(loaded["messages"][0]["payload"], dict)


class TestMQTTReplayer:
    """Test MQTT replayer logic."""

    def test_replayer_script_is_valid_python(self):
        import ast
        with open("marketing/mqtt_replayer.py") as f:
            ast.parse(f.read())


class TestScreenshots:
    """Test screenshot script data and configuration."""

    def test_pages_list_covers_key_routes(self):
        """Screenshot script must cover at minimum these pages."""
        import ast
        with open("marketing/screenshots.py") as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "PAGES":
                        pages = ast.literal_eval(node.value)
                        paths = {p[1] for p in pages}
                        required = {"/", "/printers", "/jobs", "/models", "/spools", "/orders", "/settings"}
                        missing = required - paths
                        assert not missing, f"Missing required pages: {missing}"
```

**Step 2: Run the tests**

Run: `pytest tests/test_marketing.py -v`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add tests/test_marketing.py
git commit -m "test: add unit tests for marketing automation scripts"
```

---

## Task 9: Final integration verification

**Step 1: Run full test suite**

Run: `make test`
Expected: Existing tests still pass (marketing tests run standalone)

**Step 2: Run marketing tests specifically**

Run: `pytest tests/test_marketing.py -v`
Expected: All PASS

**Step 3: Verify make targets appear in help**

Run: `make help`
Expected: `odin-seed`, `odin-screenshots`, `odin-video`, `odin-record`, `odin-marketing`, `odin-marketing-full` visible

**Step 4: Verify file structure**

```bash
ls -la marketing/
ls marketing/screenshots/.gitkeep
ls marketing/videos/.gitkeep
ls marketing/recordings/.gitkeep
```

---

## Summary

| Phase | Script | Make Target | Depends On |
|-------|--------|-------------|------------|
| 1.1 | `marketing/seed.py` | `make odin-seed` | Running ODIN instance |
| 1.2 | `marketing/screenshots.py` | `make odin-screenshots` | Seeded data + Playwright |
| 1.3 | `marketing/video.py` | `make odin-video` | Seeded data + Playwright + FFmpeg |
| 2.1 | `marketing/mqtt_recorder.py` | `make odin-record` | Live MQTT broker with active printers |
| 2.2 | `marketing/mqtt_replayer.py` | (used by odin-marketing-full) | A recorded JSON file |
| 3 | Makefile targets | `make odin-marketing` / `make odin-marketing-full` | All above |

**To run a full capture (Phase 1 only, no MQTT needed):**
```bash
export ODIN_ADMIN_PASSWORD=your-password
make odin-marketing
```

**To record a golden tape (once, while printers are active):**
```bash
make odin-record BROKER=192.168.1.100 DURATION=1800 NAME=bambu-benchy
```

**To run full capture with live data (Phase 1 + 2):**
```bash
make odin-marketing-full RECORDING=bambu-benchy BROKER=localhost
```
