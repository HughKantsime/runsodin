#!/bin/sh
set -eu

if [ "${ODIN_DEMO_EDU_SEED:-0}" = "1" ]; then
    python3 -m scripts.demo_seed_edu --db-path "${DATABASE_PATH:-/data/odin.db}"
    echo "  ✓ EDU sandbox personas seeded"
fi
