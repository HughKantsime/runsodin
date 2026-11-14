#!/bin/bash
set -Eeuo pipefail

# ITAR-PROXIES-BEGIN
# ITAR mode does not use a proxy: every connection is made directly.
# Remove the proxy settings here, before anything else runs, so that every program this
# container starts (supervisord, the ODIN services, go2rtc, ffmpeg) starts without them.
# backend/core/itar.py does the same inside each ODIN process, with the same rule
# (neutralize_environment_proxies). Names are printed, never values.
# ODIN_ITAR_MODE is read here exactly as backend/core/itar.py reads it (itar_flag_state):
# trim the six ASCII white-space characters, lower-case A-Z, then compare.
#   on:  1 true yes on          off: (unset or empty) 0 false no off
# Any other value is refused, here and in every ODIN process, with the same message. Before,
# this block and Python each read such a value in their own way: a value like "1" in quotes
# removed the proxies and announced ITAR mode here while the backend ran with it off.
odin_itar_on=0
odin_itar_space=$' \t\n\r\v\f'   # the six characters are listed, so no locale changes what is trimmed
odin_itar_mode="$(LC_ALL=C; v="${ODIN_ITAR_MODE:-}"; v="${v#"${v%%[!"${odin_itar_space}"]*}"}"; v="${v%"${v##*[!"${odin_itar_space}"]}"}"; printf '%s' "${v}" | LC_ALL=C tr 'A-Z' 'a-z')"
case "${odin_itar_mode}" in
    ""|0|false|no|off) ;;
    1|true|yes|on) ;;
    *)
        echo "  ✗ ODIN_ITAR_MODE has a value O.D.I.N. does not understand. Use 1, true, yes or on to turn ITAR mode on; use 0, false, no or off, or leave it unset, for off. Refusing to start." >&2
        exit 1
        ;;
esac
case "${odin_itar_mode}" in
    1|true|yes|on)
        odin_itar_on=1
        odin_ignored_proxies=""
        for odin_variable in $(compgen -e | LC_ALL=C sort); do
            case "$(printf '%s' "${odin_variable}" | tr '[:upper:]' '[:lower:]')" in
                no_proxy) ;;
                *_proxy)
                    if [ -n "$(printf '%s' "${!odin_variable}" | tr -d '[:space:]')" ]; then
                        odin_ignored_proxies="${odin_ignored_proxies:+${odin_ignored_proxies},}${odin_variable}"
                    fi
                    unset "${odin_variable}"
                    ;;
            esac
        done
        export NO_PROXY='*' no_proxy='*'
        if [ -n "${odin_ignored_proxies}" ]; then
            export ODIN_ITAR_IGNORED_PROXIES="${odin_ignored_proxies}"
            echo "  ! ODIN_ITAR_MODE=1: ignoring the proxy settings ${odin_ignored_proxies//,/, }. ITAR mode does not use a proxy."
        fi
        unset odin_ignored_proxies odin_variable
        ;;
esac
unset odin_itar_mode odin_itar_space
# ITAR-PROXIES-END

# This availability flag is overwritten by trusted startup, never accepted as
# an identity. The reader must still validate the root-owned runtime record.
export ODIN_AMS_RUNTIME_READY=0
# One id per container start, inherited by every supervisord program: the camera relay
# secret rotates at most once per boot (modules/printers/camera_relay.py).
ODIN_BOOT_ID="$(cat /proc/sys/kernel/random/uuid)"
export ODIN_BOOT_ID
export ODIN_BRANDING_DIRECTORY=/data/static/branding
if /usr/local/bin/python3 -I -S /usr/local/libexec/odin-runtime-identity.py; then
    export ODIN_AMS_RUNTIME_READY=1
fi

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
        (
            umask 077
            printf '%s\n' "${ENCRYPTION_KEY}" > /data/.encryption_key
        )
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
        (
            umask 077
            printf '%s\n' "${JWT_SECRET_KEY}" > /data/.jwt_secret
        )
        echo "  ✓ Generated JWT secret"
    fi
fi

if [ ! -f /data/.odin-install-id ]; then
    (
        umask 077
        python3 -c "import uuid; print(uuid.uuid4())" > /data/.odin-install-id
    )
    echo "  ✓ Generated installation ID"
else
    echo "  ✓ Installation ID present"
fi

mkdir -p /data/backups /data/uploads /data/static/branding /data/vision_frames /data/vision_models /data/run

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

if [ ! -L /app/backend/static/branding ] || [ "$(readlink /app/backend/static/branding)" != /data/static/branding ]; then
    echo "Branding storage layout unavailable" >&2
    exit 1
fi

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
        /usr/sbin/runuser -u odin -- python3 -m modules.system.restore_coordinator --rollback || true
    fi
    exit "${restore_exit_code}"
}
trap rollback_restore_on_startup_error ERR

# Every database step below runs as the app user: hand it /data (the keys and directories
# above were created by root) before any restore or migration writes there.
# Even a no-op chown changes ctime; preserve receipts on already-owned media.
find /data \( ! -user odin -o ! -group odin \) -exec chown odin:odin {} + 2>/dev/null || true

if [ "${ODIN_DB_BOOTSTRAP_OWNER:-1}" = "1" ]; then
    case "${DATABASE_URL}" in
        sqlite:///*)
            /usr/sbin/runuser -u odin -- python3 -m modules.system.restore_coordinator
            if [ -f "$(dirname "${DATABASE_PATH}")/restore-pending.json" ]; then
                restore_is_provisional=1
            fi
            ;;
        postgresql://*|postgres://*)
            # As the app user: mounted secrets are owned by it, and the preserved029
            # restore stage requires credentials owned by the process that reads them.
            /usr/sbin/runuser -u odin -- python3 -m modules.system.restore_coordinator
            ;;
    esac
    # As the app user on every dialect: the PostgreSQL restore gate accepts a data
    # directory owned by root or by the reading process, and /data is odin's.
    ODIN_DB_ROLE=bootstrap /usr/sbin/runuser -u odin -- python3 -m scripts.bootstrap_database
    echo "  ✓ Upgrade migrations complete"
    /bin/sh /app/seed_edu_if_enabled.sh
    case "${DATABASE_URL}" in
        sqlite:///*)
            /usr/sbin/runuser -u odin -- python3 -m modules.system.restore_coordinator --finalize
            restore_is_provisional=0
            ;;
    esac
else
    # Split services (monitors, vision, reports) only read the schema: as the app user,
    # for the same restore-gate ownership rule.
    /usr/sbin/runuser -u odin -- python3 -m scripts.wait_for_database_schema
fi

trap - ERR

find /data \( ! -user odin -o ! -group odin \) -exec chown odin:odin {} + 2>/dev/null || true
mkdir -p /var/run
chown odin:odin /var/run 2>/dev/null || true
/usr/sbin/runuser -u odin -- /usr/local/bin/python3 -I -S /usr/local/libexec/odin-branding-defaults.py

echo "========================================="
echo "  O.D.I.N. is ready!"
echo "========================================="

if [ "$#" -eq 0 ]; then
    set -- /usr/bin/supervisord -n -c /etc/supervisor/conf.d/odin.conf
fi
# The database guardian's first status, so one that dies before its first write still goes stale
# on /health and gets reported, instead of looking "pending" forever. 300 is its check interval.
case "${DATABASE_URL}:$*" in
    sqlite:///*/etc/supervisor/conf.d/odin.conf*)
        guardian_status="$(dirname "${DATABASE_PATH}")/run/db-guardian.json"
        if [ ! -e "${guardian_status}" ]; then
            /usr/sbin/runuser -u odin -- /bin/sh -c 'mkdir -p "$(dirname "$1")" && printf "{\"started_at\": \"%s\", \"interval_seconds\": 300}\n" "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" > "$1" && chmod 600 "$1"' sh "${guardian_status}"
        fi
        ;;
esac
case "$*" in
    */etc/supervisor/conf.d/api.conf*|*/etc/supervisor/conf.d/odin.conf*)
        # The API owns the per-boot camera relay secret (it also derives go2rtc's RTSP
        # and API credentials): rotate it once here, before supervisord, so uvicorn's
        # workers never rotate it again. Single container: go2rtc then starts with this
        # boot's config and the API's start-up sync restarts nothing. Enterprise
        # (api.conf, go2rtc its own service): the config on the shared volume stays
        # pending and the API's start-up sync reloads go2rtc. Monitors, vision and
        # reports never rotate.
        # ITAR mode: go2rtc must never start with a configuration written before this boot
        # (it can name a camera outside the local network, from before ITAR mode was turned
        # on). When the preparation fails, the script replaces the file with one that has
        # no camera sources and exits 10; any other failure refuses to start the container.
        if (cd /app/backend && /usr/sbin/runuser -u odin -- python3 -m scripts.prepare_go2rtc_config); then
            export ODIN_CAMERA_RELAY_SECRET_ROTATED=1
        else
            odin_go2rtc_status=$?
            if [ "${odin_itar_on}" = "1" ] && [ "${odin_go2rtc_status}" != "10" ]; then
                echo "  ✗ ODIN_ITAR_MODE=1: the camera (go2rtc) configuration could not be prepared, and go2rtc could still use a camera source O.D.I.N. has not checked (the reason is in the lines above)." >&2
                echo "    O.D.I.N. will not start like that. Free space on, or fix the permissions of, the go2rtc configuration folder, or unset GO2RTC_CONFIG, then start again." >&2
                exit 1
            fi
        fi
        ;;
esac
exec "$@"
