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
        export JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_bytes(32).hex())")
        echo "$JWT_SECRET_KEY" > /data/.jwt_secret
        chmod 600 /data/.jwt_secret
        echo "  ✓ Generated new JWT secret (saved to /data/.jwt_secret)"
    fi
fi

# ── Generate installation ID on first boot ──
if [ ! -f /data/.odin-install-id ]; then
    python3 -c "import uuid; print(uuid.uuid4())" > /data/.odin-install-id
    chmod 600 /data/.odin-install-id
    echo "  ✓ Generated installation ID: $(cat /data/.odin-install-id)"
else
    echo "  ✓ Installation ID: $(cat /data/.odin-install-id)"
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
chmod 600 /app/backend/.env

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
from models import Base
from sqlalchemy import create_engine
engine = create_engine('${DATABASE_URL:-sqlite:////data/odin.db}')
Base.metadata.create_all(bind=engine)
print('  ✓ Database initialized')
"

# ── Normalize enum values to lowercase (SQLAlchemy 2.x uses values, not names) ──
python3 << 'ENUMMIGREOF'
import sqlite3
conn = sqlite3.connect("/data/odin.db")
c = conn.cursor()

# jobs.status: normalize uppercase names to lowercase values
c.execute("""UPDATE jobs SET status = LOWER(status)
             WHERE status != LOWER(status)
             AND LOWER(status) IN ('pending','scheduled','printing','paused','completed','failed','cancelled')""")
jobs_fixed = c.rowcount

# spools.status: normalize uppercase names to lowercase values
try:
    c.execute("""UPDATE spools SET status = LOWER(status)
                 WHERE status != LOWER(status)
                 AND LOWER(status) IN ('active','empty','archived')""")
    spools_fixed = c.rowcount
except Exception:
    spools_fixed = 0

# orders.status: normalize uppercase names to lowercase values
try:
    c.execute("""UPDATE orders SET status = LOWER(status)
                 WHERE status != LOWER(status)
                 AND LOWER(status) IN ('pending','in_progress','partial','fulfilled','shipped','cancelled')""")
    orders_fixed = c.rowcount
except Exception:
    orders_fixed = 0

# filament_slots/spools/jobs filament_type: only 'EMPTY' needs lowercase (others like PLA match name=value)
for tbl in ('filament_slots', 'spools', 'jobs'):
    try:
        c.execute(f"UPDATE {tbl} SET filament_type = 'empty' WHERE filament_type = 'EMPTY'")
    except Exception:
        pass

conn.commit()
conn.close()
total = jobs_fixed + spools_fixed + orders_fixed
if total > 0:
    print(f"  ✓ Enum migration: normalized {total} rows (jobs={jobs_fixed}, spools={spools_fixed}, orders={orders_fixed})")
else:
    print("  ✓ Enum values already normalized")
ENUMMIGREOF

# ── Run module-owned SQL migrations (creates raw-SQL tables not managed by SQLAlchemy) ──
# Execution order: core migrations first (users table is FK target), then all module migrations.
# All SQL files use CREATE TABLE IF NOT EXISTS — safe to run on both fresh and existing databases.
python3 -c "
import sys
sys.path.insert(0, '/app/backend')
from pathlib import Path
from core.db import run_core_migrations, run_module_migrations
db_url = '${DATABASE_URL:-sqlite:////data/odin.db}'
run_core_migrations(database_url=db_url)
run_module_migrations(Path('/app/backend/modules'), database_url=db_url)
print('  ✓ Module migrations complete')
"

# ── Upgrade migrations: add columns to existing databases ──
# These ALTER TABLE statements are safe to run repeatedly (exceptions are swallowed).
# They ensure older installations gain new columns without requiring a full re-init.
python3 << 'UPGRADESEOF'
import sqlite3
conn = sqlite3.connect("/data/odin.db")

# users: MFA columns (added in v1.x)
for col, coldef in [
    ("mfa_enabled", "BOOLEAN DEFAULT 0"),
    ("mfa_secret", "TEXT"),
]:
    try:
        conn.execute(f"ALTER TABLE users ADD COLUMN {col} {coldef}")
    except Exception:
        pass  # column already exists

# users: quota and theme columns (added in v1.x)
for col, coldef in [
    ("quota_grams", "REAL"),
    ("quota_hours", "REAL"),
    ("quota_jobs", "INTEGER"),
    ("quota_period", "VARCHAR(20) DEFAULT 'monthly'"),
    ("theme_json", "TEXT"),
]:
    try:
        conn.execute(f"ALTER TABLE users ADD COLUMN {col} {coldef}")
    except Exception:
        pass

# users: group_id (added when orgs feature shipped)
try:
    conn.execute("SELECT group_id FROM users LIMIT 1")
except Exception:
    conn.execute("ALTER TABLE users ADD COLUMN group_id INTEGER REFERENCES groups(id)")

# groups: org columns (added when multi-org support shipped)
for col, coldef in [
    ("is_org", "BOOLEAN DEFAULT 0"),
    ("branding_json", "TEXT"),
    ("settings_json", "TEXT"),
]:
    try:
        conn.execute(f"ALTER TABLE groups ADD COLUMN {col} {coldef}")
    except Exception:
        pass

# printers/models/spools: org_id for resource scoping
for tbl in ["printers", "models", "spools"]:
    try:
        conn.execute(f"ALTER TABLE {tbl} ADD COLUMN org_id INTEGER REFERENCES groups(id)")
    except Exception:
        pass

# printers: various capability columns
for col, coldef in [
    ("shared", "BOOLEAN DEFAULT 0"),
    ("fan_speed", "INTEGER"),
    ("tags", "TEXT DEFAULT '[]'"),
    ("timelapse_enabled", "INTEGER DEFAULT 0"),
    ("bed_x_mm", "REAL"),
    ("bed_y_mm", "REAL"),
    ("machine_type", "TEXT"),
]:
    try:
        conn.execute(f"ALTER TABLE printers ADD COLUMN {col} {coldef}")
    except Exception:
        pass

# jobs: scheduling and chargeback columns
for col, coldef in [
    ("charged_to_user_id", "INTEGER"),
    ("charged_to_org_id", "INTEGER"),
    ("model_revision_id", "INTEGER"),
    ("required_tags", "TEXT DEFAULT '[]'"),
    ("queue_position", "INTEGER"),
    ("target_type", "TEXT DEFAULT 'specific'"),
    ("target_filter", "TEXT"),
]:
    try:
        conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coldef}")
    except Exception:
        pass

# print_jobs: columns added over time
existing_pj = {r[1] for r in conn.execute("PRAGMA table_info(print_jobs)")}
for col, col_type in [
    ("job_id", "TEXT"),
    ("filename", "TEXT"),
    ("total_layers", "INTEGER"),
    ("current_layer", "INTEGER"),
    ("remaining_minutes", "REAL"),
    ("bed_temp_target", "REAL"),
    ("nozzle_temp_target", "REAL"),
    ("filament_slots", "TEXT"),
    ("error_code", "TEXT"),
    ("scheduled_job_id", "INTEGER"),
    ("created_at", "DATETIME DEFAULT (datetime('now'))"),
    ("model_revision_id", "INTEGER"),
]:
    if col not in existing_pj:
        conn.execute(f"ALTER TABLE print_jobs ADD COLUMN {col} {col_type}")
        print(f"  ✓ Migrated print_jobs: added {col}")

# print_files: columns added over time
existing_pf = {r[1] for r in conn.execute("PRAGMA table_info(print_files)")}
for col, col_type in [
    ("bed_x_mm", "REAL"),
    ("bed_y_mm", "REAL"),
    ("compatible_api_types", "TEXT"),
    ("file_hash", "TEXT"),
    ("plate_count", "INTEGER DEFAULT 1"),
]:
    if col not in existing_pf:
        conn.execute(f"ALTER TABLE print_files ADD COLUMN {col} {col_type}")
        print(f"  ✓ Migrated print_files: added {col}")

# print_archives: columns added over time
existing_pa = {r[1] for r in conn.execute("PRAGMA table_info(print_archives)")}
for col, col_type in [
    ("tags", "TEXT DEFAULT ''"),
    ("plate_count", "INTEGER DEFAULT 1"),
    ("plate_thumbnails", "TEXT"),
    ("print_file_id", "INTEGER"),
    ("project_id", "INTEGER"),
    ("energy_kwh", "REAL"),
    ("energy_cost", "REAL"),
    ("consumption_json", "TEXT"),
    ("file_hash", "TEXT"),
]:
    if col not in existing_pa:
        conn.execute(f"ALTER TABLE print_archives ADD COLUMN {col} {col_type}")
        print(f"  ✓ Migrated print_archives: added {col}")

# spools: columns added over time
existing_sp = {r[1] for r in conn.execute("PRAGMA table_info(spools)")}
for col, col_type in [
    ("pa_profile", "TEXT"),
    ("low_stock_threshold_g", "INTEGER DEFAULT 50"),
    ("spoolman_spool_id", "INTEGER"),
]:
    if col not in existing_sp:
        conn.execute(f"ALTER TABLE spools ADD COLUMN {col} {col_type}")
        print(f"  ✓ Migrated spools: added {col}")

# vision_settings: build_plate_empty columns (added in Vigil v1.x)
try:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(vision_settings)").fetchall()]
    if 'build_plate_empty_enabled' not in cols:
        conn.execute("ALTER TABLE vision_settings ADD COLUMN build_plate_empty_enabled INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE vision_settings ADD COLUMN build_plate_empty_threshold REAL DEFAULT 0.70")
        print("  ✓ Migrated vision_settings: added build_plate_empty columns")
except Exception:
    pass

conn.commit()
conn.close()
print("  ✓ Upgrade migrations complete")
UPGRADESEOF

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

# ── Fix ownership so supervisord and all processes run as odin ──
# entrypoint.sh runs as root so secrets/DB init can complete before dropping privileges.
# supervisord.conf sets user=odin so all child processes run as non-root.
chown -R odin:odin /data 2>/dev/null || true
chown -R odin:odin /app 2>/dev/null || true
mkdir -p /var/run
chown odin:odin /var/run 2>/dev/null || true
echo "  ✓ Ownership set for odin user"

# ── Start supervisord (manages all processes) ──
exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/odin.conf
