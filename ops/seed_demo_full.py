#!/usr/bin/env python3
"""
O.D.I.N. Demo Server — Full Data Seed (Wipe & Replace)
======================================================
Generates a comprehensive, realistic dataset for a large print farm demo.
Run inside the container:
    docker cp ops/seed_demo_full.py odin:/ && docker exec odin python3 /seed_demo_full.py
"""

import base64
import json
import hashlib
import random
import sqlite3
import string
import sys
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DB_PATH = "/data/odin.db"
LICENSE_OUTPUT = "/data/odin.license"
NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
random.seed(42)  # reproducible

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def iso(dt):
    """Format datetime as ISO string."""
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return str(dt)

def rand_dt(days_ago_max, days_ago_min=0):
    """Random datetime between days_ago_max and days_ago_min days ago."""
    delta = random.uniform(days_ago_min * 86400, days_ago_max * 86400)
    return NOW - timedelta(seconds=delta)

def bcrypt_hash(password):
    """Hash password with bcrypt via passlib."""
    try:
        from passlib.context import CryptContext
        ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
        return ctx.hash(password)
    except ImportError:
        # Fallback: use hashlib (won't work with real auth but allows insert)
        print("  ⚠ passlib not found, using dummy hash")
        return "$2b$12$dummy" + hashlib.sha256(password.encode()).hexdigest()[:50]

# ---------------------------------------------------------------------------
# License generation
# ---------------------------------------------------------------------------

def generate_license():
    """Generate a valid Enterprise license signed with the host key."""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        print("  ⚠ cryptography not installed, skipping license generation")
        return

    key_path = "/root/.odin-keys/odin_private.pem"
    try:
        with open(key_path, "rb") as f:
            private_key = load_pem_private_key(f.read(), password=None)
    except FileNotFoundError:
        print(f"  ⚠ Private key not found at {key_path}, skipping license")
        return

    payload = {
        "licensee": "ODIN Demo Farm",
        "email": "demo@odin.local",
        "tier": "enterprise",
        "max_printers": 100,
        "max_users": 50,
        "issued_at": NOW.isoformat(),
        "expires_at": f"{(TODAY + timedelta(days=365)).isoformat()}T23:59:59Z",
        "features": [
            "dashboard", "cameras", "scheduling", "spool_tracking",
            "unlimited_printers", "rbac", "sso", "orders", "products", "bom",
            "webhooks", "analytics", "csv_export", "white_label", "branding",
            "permissions", "mqtt_republish", "prometheus", "smart_plug",
            "quiet_hours", "energy_tracking", "utilization_report",
            "maintenance", "ntfy", "telegram", "email_notifications",
            "push_notifications", "job_approval", "class_sections",
            "print_quotas", "usage_reports", "opcua", "audit_export",
            "sqlcipher", "custom_integration",
        ],
    }

    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_bytes = payload_json.encode("utf-8")
    signature = private_key.sign(payload_bytes)

    # Write as dot-separated urlsafe base64 (what license_manager expects)
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode()
    sig_b64 = base64.urlsafe_b64encode(signature).decode()

    with open(LICENSE_OUTPUT, "w") as f:
        f.write(f"{payload_b64}.{sig_b64}")
    print(f"  ✓ License written to {LICENSE_OUTPUT}")

# ---------------------------------------------------------------------------
# Data definitions
# ---------------------------------------------------------------------------

USERS = [
    ("demo",   "demo",     "demo@odin.local",        "admin"),
    ("sarah",  "sarah123", "sarah@printfarm.local",   "operator"),
    ("mike",   "mike123",  "mike@printfarm.local",    "operator"),
    ("alex",   "alex123",  "alex@printfarm.local",    "viewer"),
    ("jordan", "jordan123","jordan@printfarm.local",   "viewer"),
]

PRINTERS = [
    # (name, nickname, model, api_type, slot_count, ip_suffix)
    # Bambu (8)
    ("Bambu X1C #1",   "Apollo",    "X1 Carbon",       "bambu",     4, "10"),
    ("Bambu X1C #2",   "Zeus",      "X1 Carbon",       "bambu",     4, "11"),
    ("Bambu X1C #3",   "Athena",    "X1 Carbon",       "bambu",     4, "12"),
    ("Bambu P1S #1",   "Hermes",    "P1S",             "bambu",     4, "13"),
    ("Bambu P1S #2",   "Artemis",   "P1S",             "bambu",     4, "14"),
    ("Bambu A1 Mini #1","Pixel",    "A1 Mini",         "bambu",     1, "15"),
    ("Bambu A1 Mini #2","Dot",      "A1 Mini",         "bambu",     1, "16"),
    ("Bambu A1 Mini #3","Nano",     "A1 Mini",         "bambu",     1, "17"),
    # Moonraker (6)
    ("Voron 2.4 #1",   "Forge",     "Voron 2.4 350",   "moonraker", 1, "20"),
    ("Voron 2.4 #2",   "Anvil",     "Voron 2.4 350",   "moonraker", 1, "21"),
    ("Voron Trident",  "Trident",   "Voron Trident",   "moonraker", 1, "22"),
    ("Kobra 3 #1",     "Cobra",     "Anycubic Kobra 3","moonraker", 1, "23"),
    ("Kobra 3 #2",     "Viper",     "Anycubic Kobra 3","moonraker", 1, "24"),
    ("Ender 5 Plus",   "BigBoy",    "Creality Ender 5+","moonraker",1, "25"),
    # PrusaLink (5)
    ("Prusa MK4 #1",   "Prague",    "Prusa MK4",       "prusalink", 1, "30"),
    ("Prusa MK4 #2",   "Bohemia",   "Prusa MK4",       "prusalink", 1, "31"),
    ("Prusa MK3S+ #1", "Legacy",    "Prusa MK3S+",     "prusalink", 1, "32"),
    ("Prusa MK3S+ #2", "Vintage",   "Prusa MK3S+",     "prusalink", 1, "33"),
    ("Prusa XL",       "Titan",     "Prusa XL 5-tool", "prusalink", 5, "34"),
    # Elegoo (3)
    ("Neptune 4 Pro #1","Neptune",  "Elegoo Neptune 4 Pro","elegoo", 1, "40"),
    ("Neptune 4 Pro #2","Poseidon", "Elegoo Neptune 4 Pro","elegoo", 1, "41"),
    ("Centauri Carbon", "Centauri", "Elegoo Centauri Carbon","elegoo",1,"42"),
]

# Explicit per-printer states (index matches PRINTERS list)
# 12 RUNNING, 8 IDLE, 2 OFFLINE — 20 online, 2 offline
STATES_POOL = [
    "RUNNING",  # Bambu X1C #1
    "RUNNING",  # Bambu X1C #2
    "RUNNING",  # Bambu X1C #3
    "RUNNING",  # Bambu P1S #1
    "IDLE",     # Bambu P1S #2
    "RUNNING",  # Bambu A1 Mini #1
    "RUNNING",  # Bambu A1 Mini #2
    "IDLE",     # Bambu A1 Mini #3
    "RUNNING",  # Voron 2.4 #1
    "IDLE",     # Voron 2.4 #2
    "RUNNING",  # Voron Trident
    "IDLE",     # Kobra 3 #1
    "IDLE",     # Kobra 3 #2
    "IDLE",     # Ender 5 Plus
    "RUNNING",  # Prusa MK4 #1
    "IDLE",     # Prusa MK4 #2
    "RUNNING",  # Prusa MK3S+ #1
    "RUNNING",  # Prusa MK3S+ #2
    "IDLE",     # Prusa XL
    "RUNNING",  # Neptune 4 Pro #1
    "OFFLINE",  # Neptune 4 Pro #2
    "OFFLINE",  # Centauri Carbon
]

FILAMENT_COLORS = {
    "Black":    "#1a1a1a", "White":   "#f5f5f5", "Red":     "#dc2626",
    "Blue":     "#2563eb", "Green":   "#16a34a", "Orange":  "#ea580c",
    "Grey":     "#6b7280", "Gold":    "#d4a017", "Yellow":  "#eab308",
    "Purple":   "#9333ea", "Pink":    "#ec4899", "Silver":  "#a8a29e",
    "Navy":     "#1e3a5f", "Teal":    "#0d9488", "Brown":   "#78350f",
    "Lime":     "#84cc16", "Beige":   "#d2b48c", "Cyan":    "#06b6d4",
}

FILAMENT_TYPES = ["PLA"] * 10 + ["PETG"] * 4 + ["ABS"] * 2 + ["TPU"] * 1 + ["ASA"] * 1

FILAMENT_LIBRARY = [
    ("Bambu Lab",   "PLA Basic",    "PLA",  "#1a1a1a", 0.025),  # Black
    ("Bambu Lab",   "PLA Basic",    "PLA",  "#f5f5f5", 0.025),  # White
    ("Bambu Lab",   "PLA Basic",    "PLA",  "#dc2626", 0.025),  # Red
    ("Bambu Lab",   "PLA Basic",    "PLA",  "#2563eb", 0.025),  # Blue
    ("Bambu Lab",   "PLA Basic",    "PLA",  "#16a34a", 0.025),  # Green
    ("Bambu Lab",   "PLA Basic",    "PLA",  "#ea580c", 0.025),  # Orange
    ("Hatchbox",    "PETG",         "PETG", "#1a1a1a", 0.030),  # Black
    ("Hatchbox",    "PETG",         "PETG", "#f5f5f5", 0.030),  # White
    ("Hatchbox",    "PETG",         "PETG", "#2563eb", 0.030),  # Blue
    ("eSUN",        "ABS+",         "ABS",  "#1a1a1a", 0.022),
    ("eSUN",        "ABS+",         "ABS",  "#f5f5f5", 0.022),
    ("NinjaTek",    "Cheetah TPU",  "TPU",  "#1a1a1a", 0.065),
    ("Bambu Lab",   "PLA Silk",     "PLA",  "#d4a017", 0.035),
    ("Bambu Lab",   "PLA Matte",    "PLA",  "#6b7280", 0.028),
    ("eSUN",        "ASA",          "ASA",  "#f5f5f5", 0.028),
]

MODELS = [
    # (name, build_time_h, filament_type, cost, markup%, units_per_bed, category)
    ("Cable Clip Set",         0.3,  "PLA",  0.10, 400, 12, "Accessories"),
    ("Keychain Tag",           0.25, "PLA",  0.08, 350, 16, "Accessories"),
    ("Phone Stand",            0.8,  "PLA",  0.35, 250, 4,  "Accessories"),
    ("Wall Hook (4-pack)",     0.5,  "PLA",  0.20, 300, 8,  "Home"),
    ("Light Switch Cover",     0.6,  "PLA",  0.25, 250, 6,  "Home"),
    ("Desk Organizer",         2.0,  "PLA",  1.50, 200, 2,  "Office"),
    ("Raspberry Pi 4 Case",    1.5,  "PETG", 1.20, 200, 2,  "Electronics"),
    ("Succulent Planter",      1.8,  "PLA",  0.90, 250, 2,  "Home"),
    ("Name Plate (Custom)",    0.7,  "PLA",  0.30, 400, 6,  "Custom"),
    ("Tool Holder Rail",       1.2,  "PETG", 0.80, 200, 2,  "Workshop"),
    ("Electronics Enclosure",  3.5,  "ABS",  3.50, 180, 1,  "Electronics"),
    ("Bracket Set (L+R)",      1.0,  "PETG", 0.60, 200, 4,  "Hardware"),
    ("Lamp Shade (Voronoi)",   4.5,  "PLA",  2.80, 200, 1,  "Home"),
    ("Drone Frame 5inch",      3.0,  "TPU",  5.00, 150, 1,  "RC"),
    ("Articulated Dragon",     2.5,  "PLA",  1.50, 300, 1,  "Toys"),
    ("Cable Management Box",   2.2,  "PLA",  1.80, 180, 1,  "Office"),
    ("Headphone Stand",        2.8,  "PLA",  2.00, 200, 1,  "Accessories"),
    ("Coaster Set (4)",        0.9,  "PLA",  0.40, 300, 4,  "Home"),
    ("Pen Holder",             1.0,  "PLA",  0.50, 250, 3,  "Office"),
    ("GoPro Mount",            0.6,  "PETG", 0.40, 250, 4,  "Accessories"),
    ("Soap Dish",              0.8,  "PETG", 0.35, 250, 3,  "Home"),
    ("Birdhouse",              3.5,  "PLA",  2.50, 200, 1,  "Outdoor"),
    ("Cookie Cutter Set",      0.4,  "PLA",  0.15, 350, 8,  "Kitchen"),
    ("Plant Label Stakes",     0.2,  "PLA",  0.05, 400, 20, "Garden"),
    ("SD Card Holder",         0.5,  "PLA",  0.20, 300, 6,  "Accessories"),
    ("Lithophane Frame",       1.5,  "PLA",  0.80, 300, 2,  "Art"),
    ("Gear Fidget Spinner",    0.7,  "PLA",  0.30, 350, 4,  "Toys"),
    ("Vase (Spiral Mode)",     1.2,  "PLA",  0.40, 300, 2,  "Home"),
    ("Raspberry Pi Zero Case", 0.8,  "PETG", 0.50, 250, 4,  "Electronics"),
    ("Board Game Insert",      5.5,  "PLA",  4.00, 150, 1,  "Games"),
]

PRODUCTS = [
    # (name, sku, price, description, component_model_indices)
    ("Cable Management Kit",  "CMK-001", 12.99, "Cable clips + management box", [0, 15]),
    ("Desk Essentials Set",   "DES-002", 24.99, "Desk organizer + pen holder + coaster set", [5, 18, 17]),
    ("Phone Stand Pro",       "PSP-003",  8.99, "Universal phone stand", [2]),
    ("Pi4 Complete Case",     "PI4-004", 14.99, "Raspberry Pi 4 case + mounting bracket", [6, 11]),
    ("Custom Keychain",       "KEY-005",  4.99, "Personalized keychain tag", [1]),
    ("Wall Hook Set (8pc)",   "WHK-006",  9.99, "Adhesive wall hooks 8-pack", [3]),
    ("Dragon Toy",            "DRG-007", 19.99, "Articulated dragon figurine", [14]),
    ("Voronoi Lamp",          "VLM-008", 34.99, "Decorative lamp shade", [12]),
    ("Home Starter Pack",     "HSP-009", 29.99, "Planter + soap dish + vase", [7, 20, 27]),
    ("Drone Frame Kit",       "DFK-010", 39.99, "5-inch drone frame in TPU", [13]),
    ("Garden Label Set",      "GLS-011",  6.99, "20 plant label stakes", [23]),
    ("GoPro Mount Pro",       "GMP-012", 11.99, "Universal GoPro mount", [19]),
    ("Board Game Organizer",  "BGO-013", 29.99, "Custom board game insert", [29]),
    ("Headphone Stand",       "HPS-014", 14.99, "Desktop headphone holder", [16]),
    ("Pi Zero Kit",           "PZ0-015", 12.99, "Pi Zero case + SD card holder", [28, 24]),
]

CUSTOMER_FIRST = ["James","Emma","Liam","Olivia","Noah","Ava","William","Sophia",
                   "Benjamin","Isabella","Lucas","Mia","Henry","Charlotte","Alexander",
                   "Amelia","Daniel","Harper","Michael","Evelyn","David","Luna",
                   "Joseph","Ella","Samuel","Grace","Owen","Chloe","Jack","Lily",
                   "Sebastian","Aria","Aiden","Riley","Matthew","Zoey","Elijah","Nora",
                   "Oliver","Hazel","Ethan","Penelope","Logan","Layla","Mason","Ellie",
                   "Jacob","Stella","Carter","Aurora"]
CUSTOMER_LAST = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
                  "Rodriguez","Martinez","Hernandez","Lopez","Wilson","Anderson","Thomas",
                  "Taylor","Moore","Jackson","Martin","Lee","White","Harris","Clark",
                  "Lewis","Robinson"]

CONSUMABLES = [
    ("Bed Adhesive Glue Stick",     "CON-001", "piece",  2.50, 24, 5,  "3DLAC"),
    ("Brass Nozzle 0.4mm",          "CON-002", "piece",  1.50, 30, 10, "E3D"),
    ("Hardened Steel Nozzle 0.4mm", "CON-003", "piece",  8.00, 12, 4,  "E3D"),
    ("PTFE Tube (1m)",              "CON-004", "piece",  3.00, 10, 3,  "Capricorn"),
    ("PEI Build Plate 256mm",       "CON-005", "piece", 18.00,  6, 2,  "Energetic"),
    ("Silicone Sock (3-pack)",      "CON-006", "pack",   4.00, 15, 5,  "Generic"),
    ("Brass Cleaning Brush",        "CON-007", "piece",  2.00,  8, 3,  "Generic"),
    ("Desiccant Pack 50g",          "CON-008", "piece",  1.00, 40, 10, "Dry & Dry"),
    ("IPA 99% (500ml)",             "CON-009", "bottle", 8.00,  5, 2,  "MG Chemicals"),
    ("Lubricant PTFE (30ml)",       "CON-010", "bottle", 6.00,  4, 2,  "Super Lube"),
    ("Scraper Blade",               "CON-011", "piece",  3.00, 10, 3,  "Generic"),
    ("Heat Block Sock (5-pack)",    "CON-012", "pack",   5.00,  8, 3,  "Generic"),
]

HMS_ERRORS = [
    ("0700_2000_0002_0001", "AMS 1: Filament runout detected",            "warning"),
    ("0700_2000_0002_0003", "AMS 1: Filament feed error — retry",         "warning"),
    ("0500_0100_0001_0001", "Nozzle clog detected during print",          "critical"),
    ("0300_0100_0001_0002", "First layer inspection warning",             "warning"),
    ("0300_8000_0001_0001", "Heatbed temperature deviation >5°C",         "warning"),
    ("0500_0400_0002_0001", "Part cooling fan speed abnormal",            "critical"),
    ("0700_2000_0002_0002", "AMS 2: Filament runout detected",            "warning"),
    ("0500_0200_0001_0001", "Nozzle temperature deviation >10°C",         "critical"),
    ("0300_0300_0001_0001", "Spaghetti detection — possible failure",     "critical"),
    ("0700_4000_0001_0001", "AMS humidity sensor reading high",           "warning"),
]

MAINTENANCE_TASKS = [
    ("Nozzle Clean",       "Clean nozzle tip and cold pull",       None,              50,   7,  0.00, 15),
    ("Bed Leveling",       "Auto or manual bed level calibration", None,             100,  14,  0.00, 10),
    ("Lubricate Rails",    "Apply PTFE lube to X/Y/Z rails",      None,             200,  30,  1.50, 20),
    ("Belt Tension Check", "Check and adjust belt tension",        None,             300,  60,  0.00, 15),
    ("Firmware Update",    "Check and apply firmware updates",     None,             500,  90,  0.00, 30),
    ("PEI Plate Clean",    "Deep clean PEI with IPA",             None,              25,   3,  0.50,  5),
    ("PTFE Tube Replace",  "Replace bowden tube",                 None,             400,  90,  3.00, 30),
    ("Hotend Service",     "Full hotend disassembly and clean",   None,             600, 120, 10.00, 60),
    ("Filament Path Clean","Clear AMS and extruder path",         "bambu",          150,  30,  0.00, 20),
    ("Probe Calibration",  "Calibrate Z-probe offset",            "prusalink",      200,  30,  0.00, 10),
]

ALERT_TEMPLATES = [
    ("PRINT_COMPLETE",      "INFO",     "Print Completed",        "Print job '{}' finished successfully on {}"),
    ("PRINT_FAILED",        "CRITICAL", "Print Failed",           "Print job '{}' failed on {}: {}"),
    ("SPOOL_LOW",           "WARNING",  "Spool Running Low",      "Spool {} is below 100g remaining ({:.0f}g left)"),
    ("MAINTENANCE_OVERDUE", "WARNING",  "Maintenance Overdue",    "{} is overdue for {} by {:.0f} hours"),
    ("PRINT_COMPLETE",      "INFO",     "Print Completed",        "Print job '{}' completed in {:.1f}h on {}"),
    ("PRINT_FAILED",        "CRITICAL", "Print Failed",           "Print job '{}' failed at {:.0f}% on {}"),
    ("SPOOL_LOW",           "WARNING",  "Low Filament Alert",     "{} filament spool has only {:.0f}g remaining"),
    ("MAINTENANCE_OVERDUE", "CRITICAL", "Maintenance Critical",   "{} has exceeded maintenance interval for {}"),
]

AUDIT_ACTIONS = ["create", "update", "delete", "login", "export"]
AUDIT_ENTITIES = ["printer", "job", "spool", "order", "user", "model", "settings"]

# ---------------------------------------------------------------------------
# Main seeding logic
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("O.D.I.N. Demo Seed — Full Wipe & Replace")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")

    # ── Wipe all tables ──
    print("\n[1/14] Wiping existing data...")
    tables = [
        "alerts", "alert_preferences", "push_subscriptions",
        "audit_logs", "spool_usage", "consumable_usage",
        "product_consumables", "order_items", "orders",
        "product_components", "products", "consumables",
        "maintenance_logs", "maintenance_tasks",
        "nozzle_lifecycle", "hms_error_history", "printer_telemetry",
        "ams_telemetry", "print_jobs", "print_files",
        "jobs", "filament_slots", "spools", "filament_library",
        "models", "scheduler_runs", "system_config",
        "printers", "users", "webhooks", "oidc_config",
    ]
    for t in tables:
        try:
            conn.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError:
            pass  # table may not exist
    conn.commit()
    # Reset autoincrement counters
    try:
        conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    print("  ✓ All tables wiped")

    # ── Users ──
    print("\n[2/14] Seeding users...")
    for username, password, email, role in USERS:
        pw_hash = bcrypt_hash(password)
        conn.execute(
            "INSERT INTO users (username, password_hash, email, role, is_active, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (username, pw_hash, email, role, iso(rand_dt(90, 30)))
        )
    conn.commit()
    print(f"  ✓ {len(USERS)} users")

    # ── Printers ──
    print("\n[3/14] Seeding printers...")
    printer_ids = []
    for i, (name, nick, model, api_type, slots, ip_suffix) in enumerate(PRINTERS):
        state = STATES_POOL[i]
        total_hours = random.uniform(50, 2000)
        total_prints = int(total_hours / random.uniform(1.5, 3.0))
        bed_temp = random.uniform(55, 60) if state == "RUNNING" else random.uniform(22, 28)
        nozzle_temp = random.uniform(200, 260) if state == "RUNNING" else random.uniform(22, 28)
        fan_speed = random.randint(50, 100) if state == "RUNNING" else 0
        last_seen = iso(NOW - timedelta(minutes=random.randint(0, 5))) if state != "OFFLINE" else iso(rand_dt(7, 1))

        conn.execute("""
            INSERT INTO printers (
                name, nickname, model, api_type, api_host, slot_count,
                is_active, gcode_state, bed_temp, nozzle_temp,
                bed_target_temp, nozzle_target_temp, fan_speed,
                total_print_hours, total_print_count,
                hours_since_maintenance, prints_since_maintenance,
                last_seen, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name, nick, model, api_type, f"192.168.1.{ip_suffix}", slots,
            1 if state != "OFFLINE" else 0, state,
            round(bed_temp, 1), round(nozzle_temp, 1),
            60.0 if state == "RUNNING" else 0,
            215.0 if state == "RUNNING" else 0,
            fan_speed,
            round(total_hours, 1), total_prints,
            round(random.uniform(10, 200), 1), random.randint(5, 80),
            last_seen,
            iso(rand_dt(180, 90)), iso(NOW),
        ))
        printer_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    print(f"  ✓ {len(PRINTERS)} printers")

    # ── Filament Library ──
    print("\n[4/14] Seeding filament library & spools...")
    fl_ids = []
    for brand, name, material, color_hex, cost_per_g in FILAMENT_LIBRARY:
        conn.execute(
            "INSERT INTO filament_library (brand, name, material, color_hex, cost_per_gram, is_custom, created_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (brand, name, material, color_hex, cost_per_g, iso(rand_dt(180, 60)))
        )
        fl_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    print(f"  ✓ {len(FILAMENT_LIBRARY)} filament library entries")

    # ── Spools ──
    spool_ids = []
    color_names = list(FILAMENT_COLORS.keys())
    for i in range(25):
        fl = random.choice(fl_ids)
        remaining = random.choice([random.uniform(5, 80)] * 4 + [random.uniform(100, 950)])  # some nearly empty
        initial = 1000.0
        color_hex = FILAMENT_LIBRARY[fl_ids.index(fl)][3]
        status = "ACTIVE" if remaining > 10 else "EMPTY"
        conn.execute("""
            INSERT INTO spools (
                filament_id, initial_weight_g, remaining_weight_g, spool_weight_g,
                color_hex, price, purchase_date, vendor, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            fl, initial, round(remaining, 1), 250.0,
            color_hex, round(random.uniform(15, 35), 2),
            iso(rand_dt(120, 10)), random.choice(["Bambu Lab", "Hatchbox", "eSUN", "Polymaker"]),
            status, iso(rand_dt(120, 10)), iso(NOW),
        ))
        spool_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    print(f"  ✓ 25 spools")

    # ── Filament Slots ──
    print("\n[5/14] Seeding filament slots...")
    slot_count = 0
    # Realistic AMS loadouts: common color combos per printer
    AMS_LOADOUTS = [
        # (color, hex, type) — common 4-slot AMS configurations
        [("Black", "#1a1a1a", "PLA"), ("White", "#f5f5f5", "PLA"), ("Red", "#dc2626", "PLA"), ("Blue", "#2563eb", "PLA")],
        [("Black", "#1a1a1a", "PLA"), ("White", "#f5f5f5", "PLA"), ("Green", "#16a34a", "PLA"), ("Orange", "#ea580c", "PLA")],
        [("Black", "#1a1a1a", "PETG"), ("White", "#f5f5f5", "PETG"), ("Blue", "#2563eb", "PETG"), ("Grey", "#6b7280", "PETG")],
        [("Grey", "#6b7280", "PLA"), ("White", "#f5f5f5", "PLA"), ("Gold", "#d4a017", "PLA"), ("Black", "#1a1a1a", "PLA")],
        [("Black", "#1a1a1a", "PLA"), ("Red", "#dc2626", "PLA"), ("Yellow", "#eab308", "PLA"), ("Blue", "#2563eb", "PLA")],
    ]
    # Single-slot common colors
    SINGLE_COLORS = [
        ("Black", "#1a1a1a", "PLA"), ("White", "#f5f5f5", "PLA"), ("Grey", "#6b7280", "PLA"),
        ("Black", "#1a1a1a", "PETG"), ("White", "#f5f5f5", "PETG"), ("Red", "#dc2626", "PLA"),
        ("Blue", "#2563eb", "PLA"), ("Orange", "#ea580c", "PLA"), ("Black", "#1a1a1a", "ABS"),
        ("Black", "#1a1a1a", "TPU"),
    ]
    for pid_idx, (name, nick, model, api_type, slots, ip_suffix) in enumerate(PRINTERS):
        pid = printer_ids[pid_idx]
        if slots >= 4:
            loadout = AMS_LOADOUTS[pid_idx % len(AMS_LOADOUTS)]
            for s in range(1, slots + 1):
                color_name, color_hex, ftype = loadout[(s-1) % len(loadout)]
                conn.execute("""
                    INSERT INTO filament_slots (
                        printer_id, slot_number, filament_type, color, color_hex, loaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (pid, s, ftype, color_name, color_hex, iso(rand_dt(30, 0))))
                slot_count += 1
        else:
            for s in range(1, slots + 1):
                color_name, color_hex, ftype = SINGLE_COLORS[pid_idx % len(SINGLE_COLORS)]
                conn.execute("""
                    INSERT INTO filament_slots (
                        printer_id, slot_number, filament_type, color, color_hex, loaded_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (pid, s, ftype, color_name, color_hex, iso(rand_dt(30, 0))))
                slot_count += 1
    conn.commit()
    print(f"  ✓ {slot_count} filament slots")

    # ── Models ──
    print("\n[6/14] Seeding models...")
    model_ids = []
    for name, build_time, ftype, cost, markup, units_bed, category in MODELS:
        n_colors = random.randint(1, 2)
        colors_req = json.dumps({
            f"slot_{j+1}": {"color": random.choice(list(FILAMENT_COLORS.keys())), "grams": round(random.uniform(5, 50), 1)}
            for j in range(n_colors)
        })
        conn.execute("""
            INSERT INTO models (
                name, build_time_hours, default_filament_type, color_requirements,
                category, cost_per_item, units_per_bed, quantity_per_bed,
                markup_percent, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name, build_time, ftype, colors_req,
            category, cost, units_bed, units_bed,
            markup, iso(rand_dt(120, 30)), iso(NOW),
        ))
        model_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    print(f"  ✓ {len(MODELS)} models")

    # ── Products & Components ──
    print("\n[7/14] Seeding products & orders...")
    product_ids = []
    for name, sku, price, desc, component_indices in PRODUCTS:
        conn.execute(
            "INSERT INTO products (name, sku, price, description, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, sku, price, desc, iso(rand_dt(90, 20)), iso(NOW))
        )
        prod_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        product_ids.append(prod_id)
        for mi in component_indices:
            conn.execute(
                "INSERT INTO product_components (product_id, model_id, quantity_needed) VALUES (?, ?, ?)",
                (prod_id, model_ids[mi], 1)
            )
    conn.commit()
    print(f"  ✓ {len(PRODUCTS)} products")

    # ── Orders & Order Items ──
    ORDER_STATUSES = (["FULFILLED"] * 30 + ["IN_PROGRESS"] * 8 + ["SHIPPED"] * 5 +
                      ["PENDING"] * 4 + ["CANCELLED"] * 3)
    random.shuffle(ORDER_STATUSES)
    order_ids = []
    total_items = 0
    platforms = ["website", "etsy", "ebay", "amazon", "local", "wholesale"]
    for i in range(50):
        status = ORDER_STATUSES[i]
        order_date = rand_dt(60, 1 if status != "PENDING" else 0)
        shipped_date = iso(order_date + timedelta(days=random.randint(1, 5))) if status == "SHIPPED" else None
        customer = f"{random.choice(CUSTOMER_FIRST)} {random.choice(CUSTOMER_LAST)}"
        email = f"{customer.split()[0].lower()}.{customer.split()[1].lower()}@{'gmail.com' if random.random() > 0.3 else 'outlook.com'}"
        revenue = round(random.uniform(5, 200), 2)
        platform = random.choice(platforms)
        platform_fees = round(revenue * random.uniform(0.05, 0.15), 2) if platform != "local" else 0
        shipping_cost = round(random.uniform(3, 12), 2) if status != "CANCELLED" else 0
        shipping_charged = round(shipping_cost + random.uniform(0, 5), 2)
        tracking = f"1Z{''.join(random.choices(string.ascii_uppercase + string.digits, k=16))}" if status == "SHIPPED" else None

        conn.execute("""
            INSERT INTO orders (
                order_number, platform, customer_name, customer_email, status,
                revenue, platform_fees, payment_fees, shipping_charged, shipping_cost,
                labor_minutes, order_date, shipped_date, tracking_number,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"ORD-{1001+i}", platform, customer, email, status,
            revenue, platform_fees, round(revenue * 0.029 + 0.30, 2),
            shipping_charged, shipping_cost,
            random.randint(5, 45), iso(order_date), shipped_date, tracking,
            iso(order_date), iso(NOW),
        ))
        oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        order_ids.append(oid)

        # 2-3 items per order
        n_items = random.randint(1, 4)
        for _ in range(n_items):
            prod = random.choice(product_ids)
            prod_idx = product_ids.index(prod)
            qty = random.randint(1, 5)
            unit_price = PRODUCTS[prod_idx][2]
            fulfilled = qty if status in ("FULFILLED", "SHIPPED") else random.randint(0, qty)
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, quantity, unit_price, fulfilled_quantity, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (oid, prod, qty, unit_price, fulfilled, iso(order_date))
            )
            total_items += 1
    conn.commit()
    print(f"  ✓ 50 orders, {total_items} order items")

    # ── Jobs (500) ──
    print("\n[8/14] Seeding 500 jobs...")
    JOB_STATUSES = (["COMPLETED"] * 390 + ["PENDING"] * 40 + ["SCHEDULED"] * 12 +
                    ["PRINTING"] * 25 + ["FAILED"] * 20 + ["CANCELLED"] * 13)
    random.shuffle(JOB_STATUSES)
    job_ids = []
    all_color_names = list(FILAMENT_COLORS.keys())
    submitters = ["demo", "sarah", "mike"]
    order_item_ids = list(range(1, total_items + 1))
    random.shuffle(order_item_ids)
    oi_cursor = 0
    sched_counter = 0  # track which printer gets the next scheduled job

    for i in range(500):
        status = JOB_STATUSES[i]
        mid = random.choice(model_ids)
        mid_idx = model_ids.index(mid)
        m = MODELS[mid_idx]
        pid = random.choice(printer_ids)
        color = random.choice(all_color_names)
        qty = random.randint(1, m[5])  # up to units_per_bed
        build_time = m[1] * (qty / max(m[5], 1))
        est_cost = round(m[3] * qty, 2)
        sugg_price = round(est_cost * (1 + m[4] / 100), 2)

        # Timestamps based on status
        if status == "COMPLETED":
            actual_end = rand_dt(90, 1)
            actual_start = actual_end - timedelta(hours=build_time * random.uniform(0.9, 1.3))
            duration = round((actual_end - actual_start).total_seconds() / 3600, 2)
            sched_start = None
            sched_end = None
        elif status == "PRINTING":
            actual_start = NOW - timedelta(hours=build_time * random.uniform(0.1, 0.7))
            actual_end = None
            duration = None
            sched_start = None
            sched_end = None
        elif status == "SCHEDULED":
            # Each scheduled job on a different printer, evenly spaced in time
            pid = printer_ids[sched_counter % len(printer_ids)]
            base_offset = 4 + (sched_counter * 3)  # 4h, 7h, 10h, 13h, ...
            sched_start = NOW + timedelta(hours=base_offset + random.uniform(-0.5, 0.5))
            sched_end = sched_start + timedelta(hours=build_time * random.uniform(0.9, 1.1))
            actual_start = None
            actual_end = None
            duration = None
            sched_counter += 1
        elif status == "FAILED":
            actual_start = rand_dt(60, 1)
            actual_end = actual_start + timedelta(hours=build_time * random.uniform(0.1, 0.8))
            duration = round((actual_end - actual_start).total_seconds() / 3600, 2)
            sched_start = None
            sched_end = None
        else:  # PENDING, CANCELLED
            actual_start = None
            actual_end = None
            duration = None
            sched_start = None
            sched_end = None

        # Link some jobs to order items
        oi_id = None
        if status in ("COMPLETED", "PRINTING", "SCHEDULED") and oi_cursor < len(order_item_ids) and random.random() > 0.6:
            oi_id = order_item_ids[oi_cursor]
            oi_cursor += 1

        fail_reason = random.choice(["Spaghetti detected", "Filament runout", "Layer shift", "Nozzle clog", "Bed adhesion failure"]) if status == "FAILED" else None

        conn.execute("""
            INSERT INTO jobs (
                model_id, item_name, quantity, status, priority, printer_id,
                scheduled_start, scheduled_end, actual_start, actual_end,
                duration_hours, colors_required, filament_type, match_score,
                is_locked, hold,
                estimated_cost, suggested_price, order_item_id, quantity_on_bed,
                submitted_by, fail_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mid, m[0], qty, status, random.randint(1, 5), pid,
            iso(sched_start) if sched_start else None,
            iso(sched_end) if sched_end else None,
            iso(actual_start) if actual_start else None,
            iso(actual_end) if actual_end else None,
            duration, json.dumps([color]), m[2],
            random.randint(60, 100),
            0, 0,
            est_cost, sugg_price, oi_id, qty,
            random.choice(submitters), fail_reason,
            iso(actual_start or sched_start or rand_dt(90, 0)), iso(NOW),
        ))
        job_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    conn.commit()
    print(f"  ✓ 500 jobs")

    # ── Print Jobs (200) — historical MQTT-tracked ──
    print("\n[9/14] Seeding print_jobs and telemetry...")
    bambu_pids = [printer_ids[i] for i, p in enumerate(PRINTERS) if p[3] == "bambu"]
    PJ_STATUSES = ["completed"] * 160 + ["running"] * 20 + ["failed"] * 15 + ["cancelled"] * 5
    random.shuffle(PJ_STATUSES)
    filenames = [f"{m[0].lower().replace(' ', '_').replace('(', '').replace(')', '')}.3mf" for m in MODELS]

    for i in range(200):
        pj_status = PJ_STATUSES[i]
        pid = random.choice(bambu_pids)
        fname = random.choice(filenames)
        total_layers = random.randint(50, 300)
        started = rand_dt(30, 0)

        if pj_status == "completed":
            duration_min = random.uniform(20, 360)
            ended = started + timedelta(minutes=duration_min)
            progress = 100.0
            cur_layer = total_layers
        elif pj_status == "running":
            progress = round(random.uniform(5, 90), 1)
            cur_layer = int(total_layers * progress / 100)
            ended = None
            duration_min = None
        elif pj_status == "failed":
            progress = round(random.uniform(5, 80), 1)
            cur_layer = int(total_layers * progress / 100)
            duration_min = random.uniform(10, 200)
            ended = started + timedelta(minutes=duration_min)
        else:  # cancelled
            progress = round(random.uniform(0, 30), 1)
            cur_layer = int(total_layers * progress / 100)
            duration_min = random.uniform(5, 60)
            ended = started + timedelta(minutes=duration_min)

        conn.execute("""
            INSERT INTO print_jobs (
                printer_id, job_id, filename, job_name, started_at, ended_at,
                status, progress_percent, remaining_minutes,
                total_layers, current_layer,
                bed_temp_target, nozzle_temp_target, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pid, f"bambu_{random.randint(10000,99999)}", fname,
            fname.replace(".3mf", "").replace("_", " ").title(),
            iso(started), iso(ended) if ended else None,
            pj_status, progress,
            random.uniform(10, 180) if pj_status == "running" else 0,
            total_layers, cur_layer,
            random.choice([55.0, 60.0, 65.0]),
            random.choice([200.0, 215.0, 220.0, 250.0]),
            "0500_0100_0001_0001" if pj_status == "failed" else None,
        ))
    conn.commit()
    print(f"  ✓ 200 print_jobs")

    # ── Printer Telemetry (~5000 rows, 48h for active printers) ──
    active_pids = [printer_ids[i] for i, p in enumerate(PRINTERS) if STATES_POOL[i] != "OFFLINE"]
    telemetry_count = 0
    # 48 hours, every 5 min = 576 points per printer. ~19 active printers = ~10,900. Let's do every 15min = ~3600.
    interval_min = 15
    points_per_printer = int(48 * 60 / interval_min)

    for pid_idx, pid in enumerate(active_pids):
        p_state = STATES_POOL[printer_ids.index(pid)]
        # Create print windows: printing for some hours, then idle
        is_printing_now = p_state == "RUNNING"
        batch = []
        for pt in range(points_per_printer):
            t = NOW - timedelta(minutes=pt * interval_min)
            # Simulate: printing during working hours (8am-10pm), idle otherwise
            hour = (t.hour + random.uniform(-0.5, 0.5)) % 24
            is_printing = (8 <= hour <= 22) and (random.random() > 0.2)
            if pt < 4:  # last hour matches actual state
                is_printing = is_printing_now

            if is_printing:
                bed = round(random.uniform(55, 65), 1)
                nozzle = round(random.uniform(200, 260), 1)
                fan = random.randint(40, 100)
            else:
                bed = round(random.uniform(22, 28), 1)
                nozzle = round(random.uniform(22, 28), 1)
                fan = 0

            batch.append((pid, bed, nozzle,
                          60.0 if is_printing else 0,
                          215.0 if is_printing else 0,
                          fan, iso(t)))

        conn.executemany(
            "INSERT INTO printer_telemetry (printer_id, bed_temp, nozzle_temp, bed_target, nozzle_target, fan_speed, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", batch
        )
        telemetry_count += len(batch)

    conn.commit()
    print(f"  ✓ {telemetry_count} telemetry rows")

    # ── HMS Error History ──
    print("\n[10/14] Seeding HMS errors, nozzle lifecycle, alerts...")
    for _ in range(25):
        pid = random.choice(bambu_pids)
        code, msg, severity = random.choice(HMS_ERRORS)
        conn.execute(
            "INSERT INTO hms_error_history (printer_id, code, message, severity, source, occurred_at) "
            "VALUES (?, ?, ?, ?, 'bambu_hms', ?)",
            (pid, code, msg, severity, iso(rand_dt(60, 0)))
        )
    conn.commit()
    print(f"  ✓ 25 HMS errors")

    # ── Nozzle Lifecycle ──
    nozzle_types = ["hardened_steel", "stainless_steel", "brass", "hardened_steel"]
    nozzle_diameters = [0.4, 0.4, 0.4, 0.6]
    nozzle_count = 0
    for pid in printer_ids:
        # 1 retired nozzle
        nt_idx = random.randint(0, 3)
        installed = rand_dt(365, 120)
        removed = installed + timedelta(days=random.randint(30, 200))
        conn.execute("""
            INSERT INTO nozzle_lifecycle (
                printer_id, nozzle_type, nozzle_diameter,
                installed_at, removed_at, print_hours_accumulated, print_count, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pid, nozzle_types[nt_idx], nozzle_diameters[nt_idx],
            iso(installed), iso(removed),
            round(random.uniform(200, 800), 1), random.randint(50, 300),
            "Replaced due to wear"
        ))
        nozzle_count += 1
        # 1 current nozzle
        nt_idx2 = random.randint(0, 3)
        conn.execute("""
            INSERT INTO nozzle_lifecycle (
                printer_id, nozzle_type, nozzle_diameter,
                installed_at, removed_at, print_hours_accumulated, print_count
            ) VALUES (?, ?, ?, ?, NULL, ?, ?)
        """, (
            pid, nozzle_types[nt_idx2], nozzle_diameters[nt_idx2],
            iso(rand_dt(60, 1)),
            round(random.uniform(10, 150), 1), random.randint(5, 60),
        ))
        nozzle_count += 1
    conn.commit()
    print(f"  ✓ {nozzle_count} nozzle lifecycle entries")

    # ── Alerts ──
    printer_names = [p[0] for p in PRINTERS]
    for _ in range(30):
        tmpl = random.choice(ALERT_TEMPLATES)
        atype, severity, title, msg_tmpl = tmpl
        pid = random.choice(printer_ids)
        pname = printer_names[printer_ids.index(pid)]
        if "Completed" in title or "completed" in title:
            msg = f"Print job '{random.choice([m[0] for m in MODELS])}' completed on {pname}"
        elif "Failed" in title:
            msg = f"Print job '{random.choice([m[0] for m in MODELS])}' failed on {pname}"
        elif "Spool" in title or "Filament" in title:
            msg = f"Filament spool running low ({random.randint(10, 80)}g remaining)"
        else:
            msg = f"{pname} is overdue for maintenance"

        conn.execute("""
            INSERT INTO alerts (
                user_id, alert_type, severity, title, message,
                is_read, is_dismissed, printer_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """, (
            random.randint(1, len(USERS)),
            atype, severity, title, msg,
            random.choice([0, 0, 1]), pid, iso(rand_dt(14, 0)),
        ))
    conn.commit()
    print(f"  ✓ 30 alerts")

    # ── Audit Logs ──
    print("\n[11/14] Seeding audit logs...")
    usernames = [u[0] for u in USERS]
    for _ in range(100):
        action = random.choice(AUDIT_ACTIONS)
        entity = random.choice(AUDIT_ENTITIES)
        user = random.choice(usernames)
        if action == "login":
            details = json.dumps({"username": user, "method": "password"})
        elif action == "create":
            details = json.dumps({"name": f"New {entity}", "created_by": user})
        elif action == "update":
            details = json.dumps({"field": "status", "old": "pending", "new": "completed", "by": user})
        elif action == "delete":
            details = json.dumps({"name": f"Deleted {entity}", "by": user})
        else:
            details = json.dumps({"format": "csv", "rows": random.randint(10, 500), "by": user})

        conn.execute("""
            INSERT INTO audit_logs (timestamp, action, entity_type, entity_id, details, ip_address)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            iso(rand_dt(30, 0)), action, entity,
            str(random.randint(1, 50)), details,
            f"192.168.1.{random.randint(2, 254)}",
        ))
    conn.commit()
    print(f"  ✓ 100 audit logs")

    # ── Maintenance Tasks & Logs ──
    print("\n[12/14] Seeding maintenance tasks & logs...")
    mt_ids = []
    for name, desc, printer_filter, interval_h, interval_d, cost, downtime in MAINTENANCE_TASKS:
        conn.execute("""
            INSERT INTO maintenance_tasks (
                name, description, printer_model_filter,
                interval_print_hours, interval_days,
                estimated_cost, estimated_downtime_min, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
        """, (name, desc, printer_filter, interval_h, interval_d, cost, downtime,
              iso(rand_dt(180, 90))))
        mt_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()

    # 40 maintenance logs
    performers = ["sarah", "mike", "demo"]
    for _ in range(40):
        pid = random.choice(printer_ids)
        task = random.choice(mt_ids)
        task_idx = mt_ids.index(task)
        task_name = MAINTENANCE_TASKS[task_idx][0]
        conn.execute("""
            INSERT INTO maintenance_logs (
                printer_id, task_id, task_name, performed_at, performed_by,
                notes, cost, downtime_minutes, print_hours_at_service
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pid, task, task_name, iso(rand_dt(90, 1)),
            random.choice(performers),
            f"Routine {task_name.lower()} completed",
            round(random.uniform(0, 15), 2),
            random.randint(5, 60),
            round(random.uniform(50, 500), 1),
        ))
    conn.commit()
    print(f"  ✓ {len(MAINTENANCE_TASKS)} tasks, 40 logs")

    # ── Consumables ──
    print("\n[13/14] Seeding consumables...")
    con_ids = []
    for name, sku, unit, cost, stock, min_stock, vendor in CONSUMABLES:
        conn.execute("""
            INSERT INTO consumables (
                name, sku, unit, cost_per_unit, current_stock, min_stock,
                vendor, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """, (name, sku, unit, cost, stock, min_stock, vendor,
              iso(rand_dt(120, 30)), iso(NOW)))
        con_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    # Product-consumable links (~20)
    pc_count = 0
    for prod_id in product_ids:
        n_cons = random.randint(1, 3)
        chosen = random.sample(con_ids, min(n_cons, len(con_ids)))
        for cid in chosen:
            conn.execute(
                "INSERT INTO product_consumables (product_id, consumable_id, quantity_per_product) VALUES (?, ?, ?)",
                (prod_id, cid, round(random.uniform(0.1, 2.0), 2))
            )
            pc_count += 1
    conn.commit()
    print(f"  ✓ {len(CONSUMABLES)} consumables, {pc_count} product links")

    # ── License ──
    print("\n[14/14] Generating enterprise license...")
    generate_license()

    conn.execute("PRAGMA foreign_keys=ON")
    conn.close()

    print("\n" + "=" * 60)
    print("Done! Dataset summary:")
    print(f"  Users:        {len(USERS)}")
    print(f"  Printers:     {len(PRINTERS)}")
    print(f"  Filament Lib: {len(FILAMENT_LIBRARY)}")
    print(f"  Spools:       25")
    print(f"  Fil. Slots:   {slot_count}")
    print(f"  Models:       {len(MODELS)}")
    print(f"  Products:     {len(PRODUCTS)}")
    print(f"  Orders:       50 ({total_items} items)")
    print(f"  Jobs:         500")
    print(f"  Print Jobs:   200")
    print(f"  Telemetry:    {telemetry_count}")
    print(f"  HMS Errors:   25")
    print(f"  Nozzles:      {nozzle_count}")
    print(f"  Alerts:       30")
    print(f"  Audit Logs:   100")
    print(f"  Maint Tasks:  {len(MAINTENANCE_TASKS)} + 40 logs")
    print(f"  Consumables:  {len(CONSUMABLES)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
