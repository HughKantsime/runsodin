"""Fixtures and helpers for the full demo seed (no database writes on import)."""
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
            "print_quotas", "usage_reports", "audit_export",
            "custom_integration",
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

