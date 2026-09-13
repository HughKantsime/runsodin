#!/bin/bash
set -Eeuo pipefail

echo "========================================="

load_secret_file() {
    secret_name="$1"
    file_variable="${secret_name}_FILE"
    eval "secret_path=\${${file_variable}:-}"
    [ -n "${secret_path}" ] || return 0
    [ -f "${secret_path}" ] || {
        echo "Required secret file for ${secret_name} is missing" >&2
        exit 1
    }
    [ ! -L "${secret_path}" ] || {
        echo "Secret file for ${secret_name} cannot be a symlink" >&2
        exit 1
    }
    secret_value=$(< "${secret_path}")
    [ -n "${secret_value}" ] || {
        echo "Secret file for ${secret_name} is empty" >&2
        exit 1
    }
    export "${secret_name}=${secret_value}"
}

load_secret_file ENCRYPTION_KEY
load_secret_file JWT_SECRET_KEY
load_secret_file API_KEY
echo "  O.D.I.N. — Starting up..."
echo "========================================="

if [ -z "${ENCRYPTION_KEY:-}" ]; then
    if [ -f /data/.encryption_key ]; then
        ENCRYPTION_KEY=$(< /data/.encryption_key)
        export ENCRYPTION_KEY
        echo "  ✓ Loaded encryption key"
    else
        ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
        export ENCRYPTION_KEY
        umask 077
        printf '%s\n' "${ENCRYPTION_KEY}" > /data/.encryption_key
        echo "  ✓ Generated encryption key"
    fi
fi

if [ -z "${JWT_SECRET_KEY:-}" ]; then
    if [ -f /data/.jwt_secret ]; then
        JWT_SECRET_KEY=$(< /data/.jwt_secret)
        export JWT_SECRET_KEY
        echo "  ✓ Loaded JWT secret"
    else
        JWT_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_bytes(32).hex())")
        export JWT_SECRET_KEY
        umask 077
        printf '%s\n' "${JWT_SECRET_KEY}" > /data/.jwt_secret
        echo "  ✓ Generated JWT secret"
    fi
fi

if [ ! -f /data/.odin-install-id ]; then
    umask 077
    python3 -c "import uuid; print(uuid.uuid4())" > /data/.odin-install-id
    echo "  ✓ Generated installation ID"
else
    echo "  ✓ Installation ID present"
fi

mkdir -p /data/backups /data/uploads /data/static/branding /data/vision_frames /data/vision_models

if [ -d /app/backend/vision_models_default ]; then
    for model in /app/backend/vision_models_default/*.onnx; do
        [ -f "${model}" ] || continue
        basename=$(basename "${model}")
        if [ ! -f "/data/vision_models/${basename}" ]; then
            cp "${model}" "/data/vision_models/${basename}"
            echo "  ✓ Copied default vision model: ${basename}"
        fi
    done
fi

ln -sfn /data/static/branding /app/backend/static/branding 2>/dev/null || true

export DATABASE_URL="${DATABASE_URL:-sqlite:////data/odin.db}"
case "${DATABASE_URL}" in
    sqlite:///*)
        export DATABASE_PATH="${DATABASE_URL#sqlite:///}"
        ;;
    postgresql://*|postgres://*)
        unset DATABASE_PATH || true
        ;;
    *)
        echo "Unsupported DATABASE_URL backend" >&2
        exit 1
        ;;
esac

cd /app/backend

# A container restart can leave Supervisor's PID file behind. PID 1 is then
# reused by this entrypoint, which would make the offline restore guard mistake
# the new startup shell for the prior ODIN process. No Supervisor process can
# be live before this entrypoint launches it, so remove only its known PID file.
unlink /var/run/supervisord.pid 2>/dev/null || true

restore_is_provisional=0
rollback_restore_on_startup_error() {
    restore_exit_code=$?
    trap - ERR
    if [ "${restore_is_provisional}" = "1" ]; then
        python3 -m modules.system.restore_coordinator --rollback || true
    fi
    exit "${restore_exit_code}"
}
trap rollback_restore_on_startup_error ERR

if [ "${ODIN_DB_BOOTSTRAP_OWNER:-1}" = "1" ]; then
    case "${DATABASE_URL}" in
        sqlite:///*)
            python3 -m modules.system.restore_coordinator
            if [ -f "$(dirname "${DATABASE_PATH}")/restore-pending.json" ]; then
                restore_is_provisional=1
            fi
            ;;
        postgresql://*|postgres://*)
            python3 -m modules.system.restore_coordinator
            ;;
    esac
    ODIN_DB_ROLE=bootstrap python3 -m scripts.bootstrap_database
    echo "  ✓ Upgrade migrations complete"
    /bin/sh /app/seed_edu_if_enabled.sh
    case "${DATABASE_URL}" in
        sqlite:///*)
            python3 -m modules.system.restore_coordinator --finalize
            restore_is_provisional=0
            ;;
    esac
else
    python3 -m scripts.wait_for_database_schema
fi

trap - ERR

chown -R odin:odin /data 2>/dev/null || true
mkdir -p /var/run
chown odin:odin /var/run 2>/dev/null || true

echo "========================================="
echo "  O.D.I.N. is ready!"
echo "========================================="

if [ "$#" -eq 0 ]; then
    set -- /usr/bin/supervisord -n -c /etc/supervisor/conf.d/odin.conf
fi
exec "$@"
