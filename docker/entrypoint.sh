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
mkdir -p /data/backups /data/uploads /data/static/branding

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
