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

# ── Initialize database (creates tables if needed) ──
cd /app/backend
python3 -c "
from models import Base, init_db
from sqlalchemy import create_engine
engine = create_engine('${DATABASE_URL:-sqlite:////data/odin.db}')
Base.metadata.create_all(bind=engine)
print('  ✓ Database initialized')
"

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

# ── Start supervisord (manages all processes) ──
exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/odin.conf
