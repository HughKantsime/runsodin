#!/bin/bash
set -e

echo "========================================="
echo "  O.D.I.N. — Starting up..."
echo "========================================="

# ── Generate secrets if not provided ──
if [ -z "$ENCRYPTION_KEY" ]; then
    if [ -f /data/.encryption_key ]; then
        export ENCRYPTION_KEY=$(cat /data/.encryption_key)
        echo "  ✓ Loaded encryption key from /data/.encryption_key"
    else
        export ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
        echo "$ENCRYPTION_KEY" > /data/.encryption_key
        chmod 600 /data/.encryption_key
        echo "  ✓ Generated new encryption key (saved to /data/.encryption_key)"
    fi
fi

if [ -z "$JWT_SECRET_KEY" ]; then
    if [ -f /data/.jwt_secret ]; then
        export JWT_SECRET_KEY=$(cat /data/.jwt_secret)
        echo "  ✓ Loaded JWT secret from /data/.jwt_secret"
    else
        export JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
        echo "$JWT_SECRET_KEY" > /data/.jwt_secret
        chmod 600 /data/.jwt_secret
        echo "  ✓ Generated new JWT secret (saved to /data/.jwt_secret)"
    fi
fi

# ── Ensure data directories exist ──
mkdir -p /data/backups /data/uploads /data/static/branding /data/vision_frames /data/vision_models

# ── Copy default vision models if not present ──
if [ -d /app/backend/vision_models_default ]; then
    for model in /app/backend/vision_models_default/*.onnx; do
        [ -f "$model" ] || continue
        basename=$(basename "$model")
        if [ ! -f "/data/vision_models/$basename" ]; then
            cp "$model" "/data/vision_models/$basename"
            echo "  ✓ Copied default vision model: $basename"
        fi
    done
fi

# ── Symlink static/branding into backend so it can serve uploaded logos ──
ln -sfn /data/static/branding /app/backend/static/branding 2>/dev/null || true

# ── Write backend .env file from environment ──
cat > /app/backend/.env <<EOF
DATABASE_URL=${DATABASE_URL:-sqlite:////data/odin.db}
ENCRYPTION_KEY=${ENCRYPTION_KEY}
JWT_SECRET_KEY=${JWT_SECRET_KEY}
API_KEY=${API_KEY:-}
CORS_ORIGINS=${CORS_ORIGINS:-http://localhost:8000,http://localhost:3000}
HOST=0.0.0.0
PORT=8000
EOF

echo "  ✓ Configuration written"

# ── Write environment file for supervisord processes ──
cat > /data/.env.supervisor <<ENVEOF
ENCRYPTION_KEY=${ENCRYPTION_KEY}
JWT_SECRET_KEY=${JWT_SECRET_KEY}
API_KEY=${API_KEY:-}
DATABASE_URL=${DATABASE_URL:-sqlite:////data/odin.db}
DATABASE_PATH=/data/odin.db
BACKEND_PATH=/app/backend
PYTHONUNBUFFERED=1
ENVEOF
chmod 600 /data/.env.supervisor
echo "  ✓ Supervisor environment written"

# ── Initialize database (creates tables if needed) ──
cd /app/backend
python3 -c "
from models import Base, init_db
from sqlalchemy import create_engine
engine = create_engine('${DATABASE_URL:-sqlite:////data/odin.db}')
Base.metadata.create_all(bind=engine)
print('  ✓ Database initialized')
"

# ── Create users table (raw SQL, not in SQLAlchemy models) ──
python3 << 'USERSEOF'
import sqlite3
conn = sqlite3.connect("/data/odin.db")
conn.execute("""CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(100) UNIQUE NOT NULL,
    email VARCHAR(200),
    password_hash VARCHAR(200) NOT NULL,
    role VARCHAR(20) DEFAULT 'viewer',
    is_active BOOLEAN DEFAULT 1,
    last_login DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    oidc_subject VARCHAR(200),
    oidc_provider VARCHAR(50)
)""")
conn.commit()
conn.close()
print("  ✓ Users table ready")
USERSEOF

# ── Create groups table (raw SQL, not in SQLAlchemy models) ──
python3 << 'GROUPSEOF'
import sqlite3
conn = sqlite3.connect("/data/odin.db")
conn.execute("""CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    owner_id INTEGER REFERENCES users(id),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")
conn.commit()

# Migration: add group_id to users table
try:
    conn.execute("SELECT group_id FROM users LIMIT 1")
except Exception:
    conn.execute("ALTER TABLE users ADD COLUMN group_id INTEGER REFERENCES groups(id)")
    conn.commit()

conn.close()
print("  ✓ Groups table ready")
GROUPSEOF

# ── Create print_jobs table (raw SQL, not in SQLAlchemy models) ──
python3 << 'PRINTJOBSEOF'
import sqlite3
conn = sqlite3.connect("/data/odin.db")
conn.execute("""CREATE TABLE IF NOT EXISTS print_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL REFERENCES printers(id),
    job_id TEXT,
    filename TEXT,
    job_name TEXT,
    started_at DATETIME NOT NULL,
    ended_at DATETIME,
    status TEXT DEFAULT 'running',
    progress_percent REAL DEFAULT 0,
    remaining_minutes REAL,
    total_layers INTEGER,
    current_layer INTEGER,
    bed_temp_target REAL,
    nozzle_temp_target REAL,
    filament_slots TEXT,
    error_code TEXT,
    scheduled_job_id INTEGER,
    created_at DATETIME DEFAULT (datetime('now'))
)""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_print_jobs_printer ON print_jobs(printer_id)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_print_jobs_status ON print_jobs(status)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_print_jobs_started ON print_jobs(started_at)")
conn.commit()
conn.close()
print("  ✓ Print jobs table ready")
PRINTJOBSEOF

# ── Create print_files table ──
python3 << 'PRINTFILESEOF'
import sqlite3
conn = sqlite3.connect("/data/odin.db")
conn.execute("""CREATE TABLE IF NOT EXISTS print_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT,
    original_filename TEXT,
    project_name TEXT,
    print_time_seconds REAL,
    total_weight_grams REAL,
    layer_count INTEGER,
    layer_height REAL,
    nozzle_diameter REAL,
    printer_model TEXT,
    supports_used BOOLEAN DEFAULT 0,
    bed_type TEXT,
    filaments_json TEXT,
    thumbnail_b64 TEXT,
    mesh_data TEXT,
    model_id INTEGER,
    job_id INTEGER,
    uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    stored_path TEXT,
    filament_weight_grams REAL
)""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_print_files_model ON print_files(model_id)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_print_files_job ON print_files(job_id)")
conn.commit()
conn.close()
print("  ✓ Print files table ready")
PRINTFILESEOF

# ── Create oidc_config table ──
python3 << 'OIDCEOF'
import sqlite3
conn = sqlite3.connect("/data/odin.db")
conn.execute("""CREATE TABLE IF NOT EXISTS oidc_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT DEFAULT 'SSO Login',
    client_id TEXT,
    client_secret_encrypted TEXT,
    tenant_id TEXT,
    discovery_url TEXT,
    scopes TEXT DEFAULT 'openid profile email',
    auto_create_users BOOLEAN DEFAULT 0,
    default_role TEXT DEFAULT 'viewer',
    is_enabled BOOLEAN DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")
conn.commit()
conn.close()
print("  ✓ OIDC config table ready")
OIDCEOF

# ── Create webhooks table ──
python3 << 'WEBHOOKSEOF'
import sqlite3
conn = sqlite3.connect("/data/odin.db")
conn.execute("""CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    webhook_type TEXT DEFAULT 'generic',
    alert_types TEXT,
    is_enabled BOOLEAN DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")
conn.execute("""CREATE TABLE IF NOT EXISTS ams_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    ams_unit INTEGER NOT NULL,
    humidity REAL,
    temperature REAL,
    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_ams_telemetry_printer ON ams_telemetry(printer_id)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_ams_telemetry_recorded ON ams_telemetry(recorded_at)")
conn.commit()
conn.close()
print("  ✓ Webhooks and AMS telemetry tables ready")
WEBHOOKSEOF

# ── Create telemetry expansion tables ──
python3 << 'TELEMETRYEOF'
import sqlite3
conn = sqlite3.connect("/data/odin.db")

conn.execute("""CREATE TABLE IF NOT EXISTS printer_telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    bed_temp REAL,
    nozzle_temp REAL,
    bed_target REAL,
    nozzle_target REAL,
    fan_speed INTEGER,
    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_printer_telemetry_printer ON printer_telemetry(printer_id)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_printer_telemetry_recorded ON printer_telemetry(recorded_at)")

conn.execute("""CREATE TABLE IF NOT EXISTS hms_error_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    message TEXT,
    severity TEXT DEFAULT 'warning',
    source TEXT DEFAULT 'bambu_hms',
    occurred_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_hms_history_printer ON hms_error_history(printer_id)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_hms_history_occurred ON hms_error_history(occurred_at)")

conn.execute("""CREATE TABLE IF NOT EXISTS nozzle_lifecycle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL,
    nozzle_type TEXT,
    nozzle_diameter REAL,
    installed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    removed_at DATETIME,
    print_hours_accumulated REAL DEFAULT 0,
    print_count INTEGER DEFAULT 0,
    notes TEXT
)""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_nozzle_lifecycle_printer ON nozzle_lifecycle(printer_id)")

# Add fan_speed column to printers if missing
try:
    conn.execute("ALTER TABLE printers ADD COLUMN fan_speed INTEGER")
except Exception:
    pass  # Column already exists

conn.commit()
conn.close()
print("  ✓ Telemetry expansion tables ready")
TELEMETRYEOF

# ── Create consumables tables ──
python3 << 'CONSUMABLESEOF'
import sqlite3
conn = sqlite3.connect("/data/odin.db")

conn.execute("""CREATE TABLE IF NOT EXISTS consumables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sku TEXT,
    unit TEXT DEFAULT 'piece',
    cost_per_unit REAL DEFAULT 0,
    current_stock REAL DEFAULT 0,
    min_stock REAL DEFAULT 0,
    vendor TEXT,
    notes TEXT,
    status TEXT DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

conn.execute("""CREATE TABLE IF NOT EXISTS product_consumables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    consumable_id INTEGER NOT NULL REFERENCES consumables(id),
    quantity_per_product REAL DEFAULT 1,
    notes TEXT
)""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_product_consumables_product ON product_consumables(product_id)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_product_consumables_consumable ON product_consumables(consumable_id)")

conn.execute("""CREATE TABLE IF NOT EXISTS consumable_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    consumable_id INTEGER NOT NULL REFERENCES consumables(id),
    order_id INTEGER,
    quantity_used REAL NOT NULL,
    used_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    notes TEXT
)""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_consumable_usage_consumable ON consumable_usage(consumable_id)")

conn.commit()
conn.close()
print("  ✓ Consumables tables ready")
CONSUMABLESEOF

# ── Create vision AI tables ──
python3 << 'VISIONEOF'
import sqlite3
conn = sqlite3.connect("/data/odin.db")

conn.execute("""CREATE TABLE IF NOT EXISTS vision_detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL REFERENCES printers(id),
    print_job_id INTEGER,
    detection_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT DEFAULT 'pending',
    frame_path TEXT,
    bbox_json TEXT,
    metadata_json TEXT,
    reviewed_by INTEGER,
    reviewed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_vision_detections_printer ON vision_detections(printer_id)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_vision_detections_type ON vision_detections(detection_type)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_vision_detections_status ON vision_detections(status)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_vision_detections_created ON vision_detections(created_at)")

conn.execute("""CREATE TABLE IF NOT EXISTS vision_settings (
    printer_id INTEGER PRIMARY KEY,
    enabled INTEGER DEFAULT 1,
    spaghetti_enabled INTEGER DEFAULT 1,
    spaghetti_threshold REAL DEFAULT 0.65,
    first_layer_enabled INTEGER DEFAULT 1,
    first_layer_threshold REAL DEFAULT 0.60,
    detachment_enabled INTEGER DEFAULT 1,
    detachment_threshold REAL DEFAULT 0.70,
    auto_pause INTEGER DEFAULT 0,
    capture_interval_sec INTEGER DEFAULT 10,
    collect_training_data INTEGER DEFAULT 0,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

conn.execute("""CREATE TABLE IF NOT EXISTS vision_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    detection_type TEXT NOT NULL,
    filename TEXT NOT NULL,
    version TEXT,
    input_size INTEGER DEFAULT 640,
    is_active INTEGER DEFAULT 0,
    metadata_json TEXT,
    uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")

conn.commit()
conn.close()
print("  ✓ Vision AI tables ready")
VISIONEOF

# ── Register default vision models if not already registered ──
python3 << 'VISIONMODELSEOF'
import sqlite3, os
conn = sqlite3.connect("/data/odin.db")
cur = conn.cursor()
cur.execute("SELECT COUNT(*) FROM vision_models")
count = cur.fetchone()[0]
if count == 0:
    model_path = "/data/vision_models/obico_spaghetti.onnx"
    if os.path.isfile(model_path):
        cur.execute(
            """INSERT INTO vision_models (name, detection_type, filename, version, input_size, is_active, uploaded_at)
            VALUES ('Obico Spaghetti Detector', 'spaghetti', 'obico_spaghetti.onnx', '1.0', 416, 1, datetime('now'))"""
        )
        conn.commit()
        print("  ✓ Registered default spaghetti detection model (Obico, 416x416)")
    else:
        print("  - No default vision models found")
else:
    print("  ✓ Vision models already registered")
conn.close()
VISIONMODELSEOF

# ── Enable SQLite WAL mode ──
python3 -c "
import sqlite3
conn = sqlite3.connect('/data/odin.db')
conn.execute('PRAGMA journal_mode=WAL')
conn.close()
print('  ✓ SQLite WAL mode enabled')
"

echo "========================================="
echo "  O.D.I.N. is ready!"
echo "  Web UI: http://localhost:8000"
echo "========================================="

# ── Inject environment into supervisord config ──
# Supervisord child processes don't inherit shell exports, so we inject them
ENV_VARS="ENCRYPTION_KEY=\"${ENCRYPTION_KEY}\",JWT_SECRET_KEY=\"${JWT_SECRET_KEY}\",API_KEY=\"${API_KEY:-}\",DATABASE_URL=\"${DATABASE_URL:-sqlite:////data/odin.db}\",DATABASE_PATH=\"/data/odin.db\",BACKEND_PATH=\"/app/backend\",PYTHONUNBUFFERED=\"1\""

sed -i "s|environment=PYTHONUNBUFFERED=\"1\"|environment=${ENV_VARS}|g" /etc/supervisor/conf.d/odin.conf
echo "  ✓ Supervisor environment injected"

# ── Start supervisord (manages all processes) ──
exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/odin.conf
