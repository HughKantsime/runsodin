#!/bin/sh
set -eu

if [ "${ODIN_DEMO_EDU_SEED:-0}" = "1" ]; then
    python3 -m scripts.demo_seed_edu --db-path "${DATABASE_PATH:-/data/odin.db}"
    echo "  ✓ EDU sandbox personas seeded"
fi

if [ "${ODIN_EDU_SANDBOX_SEED:-0}" = "1" ]; then
    python3 -m scripts.seed_edu_sandbox \
        --db-path "${DATABASE_PATH:-/data/odin.db}" \
        --sandbox-id "${ODIN_EDU_SANDBOX_ID:?sandbox ID required}"
    echo "  ✓ EDU sandbox classroom graph seeded"
fi
