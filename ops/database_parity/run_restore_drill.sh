#!/bin/bash
set -Eeuo pipefail

image="${ODIN_PARITY_IMAGE:-odin-dbparity:local}"
expected_image_id="${ODIN_PARITY_IMAGE_ID:-}"
postgres_image="postgres@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685"
suffix="${ODIN_PARITY_RESOURCE_SUFFIX:-$$}"
if [[ ! "${suffix}" =~ ^[a-z0-9][a-z0-9-]{1,63}$ ]]; then
    echo "ODIN_PARITY_RESOURCE_SUFFIX is invalid" >&2
    exit 2
fi
network="odin-pg-restore-net-${suffix}"
postgres_container="odin-pg-restore-${suffix}"
data_volume="odin-pg-restore-data-${suffix}"
api_container="odin-pg-api-${suffix}"
worker_container="odin-pg-worker-${suffix}"
application_volume="odin-pg-app-data-${suffix}"
# Docker Desktop can only bind files from shared host paths. Keep this
# short-lived secret inside the checked-out workspace and ignore it in git.
admin_password_file="$(mktemp "${PWD}/.odin-pg-restore.XXXXXX")"
app_password_file="$(mktemp "${PWD}/.odin-pg-restore.XXXXXX")"
maintenance_password_file="$(mktemp "${PWD}/.odin-pg-restore.XXXXXX")"
runtime_secret_file="$(mktemp "${PWD}/.odin-pg-restore.XXXXXX")"
step="initialize"

cleanup() {
    docker rm -f "${worker_container}" >/dev/null 2>&1 || true
    docker rm -f "${api_container}" >/dev/null 2>&1 || true
    docker rm -f "${postgres_container}" >/dev/null 2>&1 || true
    docker network rm "${network}" >/dev/null 2>&1 || true
    docker volume rm "${data_volume}" >/dev/null 2>&1 || true
    docker volume rm "${application_volume}" >/dev/null 2>&1 || true
    unlink "${admin_password_file}" >/dev/null 2>&1 || true
    unlink "${app_password_file}" >/dev/null 2>&1 || true
    unlink "${maintenance_password_file}" >/dev/null 2>&1 || true
    unlink "${runtime_secret_file}" >/dev/null 2>&1 || true
}

on_error() {
    code=$?
    printf 'database parity restore drill failed at %s (exit %s)\n' "${step}" "${code}" >&2
    docker logs --tail 60 "${postgres_container}" 2>/dev/null || true
    docker logs --tail 60 "${api_container}" 2>/dev/null || true
    docker logs --tail 60 "${worker_container}" 2>/dev/null || true
    exit "${code}"
}

trap on_error ERR
trap cleanup EXIT

if [ -n "${ODIN_PARITY_SECRET_INPUT_FILE:-}" ]; then
    if [ ! -f "${ODIN_PARITY_SECRET_INPUT_FILE}" ]; then
        echo "ODIN parity secret input file does not exist" >&2
        exit 2
    fi
    secret_value() {
        sed -n "s/^${1}=//p" "${ODIN_PARITY_SECRET_INPUT_FILE}" | tail -n 1
    }
    secret_postgres_admin="$(secret_value POSTGRES_ADMIN_PASSWORD)"
    secret_postgres_app="$(secret_value POSTGRES_APP_PASSWORD)"
    secret_postgres_maintenance="$(secret_value POSTGRES_MAINTENANCE_PASSWORD)"
    secret_api_key="$(secret_value API_KEY)"
    secret_jwt="$(secret_value JWT_SECRET_KEY)"
    secret_encryption="$(secret_value ENCRYPTION_KEY)"
    secret_admin="$(secret_value ODIN_CANDIDATE_ADMIN_PASSWORD)"
    secret_operator="$(secret_value ODIN_CANDIDATE_OPERATOR_PASSWORD)"
    secret_viewer="$(secret_value ODIN_CANDIDATE_VIEWER_PASSWORD)"
    for value in \
        "${secret_postgres_admin}" "${secret_postgres_app}" \
        "${secret_postgres_maintenance}" "${secret_api_key}" \
        "${secret_jwt}" "${secret_encryption}" "${secret_admin}" \
        "${secret_operator}" "${secret_viewer}"; do
        if [ -z "${value}" ]; then
            echo "ODIN parity secret input file is incomplete" >&2
            exit 2
        fi
    done
else
    secret_postgres_admin="$(openssl rand -hex 24)"
    secret_postgres_app="$(openssl rand -hex 24)"
    secret_postgres_maintenance="$(openssl rand -hex 24)"
    secret_api_key="$(openssl rand -hex 32)"
    secret_jwt="$(openssl rand -hex 48)"
    secret_encryption="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"
    secret_admin="Candidate-Admin-Aa1-$(openssl rand -hex 12)"
    secret_operator="Candidate-Operator-Aa1-$(openssl rand -hex 12)"
    secret_viewer="Candidate-Viewer-Aa1-$(openssl rand -hex 12)"
fi
printf '%s\n' "${secret_postgres_admin}" > "${admin_password_file}"
printf '%s\n' "${secret_postgres_app}" > "${app_password_file}"
printf '%s\n' "${secret_postgres_maintenance}" > "${maintenance_password_file}"
chmod 0600 "${admin_password_file}" "${app_password_file}" "${maintenance_password_file}"
{
    printf 'API_KEY=%s\n' "${secret_api_key}"
    printf 'JWT_SECRET_KEY=%s\n' "${secret_jwt}"
    printf 'ENCRYPTION_KEY=%s\n' "${secret_encryption}"
    printf 'ODIN_CANDIDATE_ADMIN_PASSWORD=%s\n' "${secret_admin}"
    printf 'ODIN_CANDIDATE_OPERATOR_PASSWORD=%s\n' "${secret_operator}"
    printf 'ODIN_CANDIDATE_VIEWER_PASSWORD=%s\n' "${secret_viewer}"
    printf 'CORS_ORIGINS=http://school.test\n'
    printf 'TRUSTED_HOSTS=school.test,localhost\n'
    printf 'COOKIE_SECURE=false\n'
} > "${runtime_secret_file}"
chmod 0600 "${runtime_secret_file}"
for password_file in \
    "${admin_password_file}" \
    "${app_password_file}" \
    "${maintenance_password_file}"; do
    docker run --rm --entrypoint test \
        --mount "type=bind,src=${password_file},dst=/run/secrets/password,readonly" \
        "${postgres_image}" \
        -s /run/secrets/password
done
docker run --rm --entrypoint test \
    --mount "type=bind,src=${runtime_secret_file},dst=/run/secrets/runtime,readonly" \
    "${image}" -s /run/secrets/runtime
docker network create "${network}" >/dev/null
docker volume create "${data_volume}" >/dev/null
docker volume create "${application_volume}" >/dev/null

step="postgres startup"
docker run -d \
    --name "${postgres_container}" \
    --network "${network}" \
    -e POSTGRES_USER=odin_admin \
    -e POSTGRES_DB=odin \
    -e POSTGRES_PASSWORD_FILE=/run/secrets/postgres_admin_password \
    -e ODIN_APP_PASSWORD_FILE=/run/secrets/postgres_app_password \
    -e ODIN_MAINTENANCE_PASSWORD_FILE=/run/secrets/postgres_maintenance_password \
    --mount "type=bind,src=${admin_password_file},dst=/run/secrets/postgres_admin_password,readonly" \
    --mount "type=bind,src=${app_password_file},dst=/run/secrets/postgres_app_password,readonly" \
    --mount "type=bind,src=${maintenance_password_file},dst=/run/secrets/postgres_maintenance_password,readonly" \
    --mount "type=bind,src=${PWD}/docker/postgres-init/10-odin-roles.sh,dst=/docker-entrypoint-initdb.d/10-odin-roles.sh,readonly" \
    --mount "type=volume,src=${data_volume},dst=/var/lib/postgresql/data" \
    "${postgres_image}" \
    >/dev/null

ready=0
for _attempt in $(seq 1 480); do
    if docker exec "${postgres_container}" sh -c \
        'read -r postmaster_pid < "$PGDATA/postmaster.pid" && test "$postmaster_pid" = 1 && psql -U odin_admin -d odin -tAc "SELECT COUNT(*) FROM pg_roles WHERE rolname IN ('"'"'odin'"'"', '"'"'odin_maintenance'"'"')" | grep -qx 2' \
        >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 0.25
done
if [ "${ready}" != "1" ]; then
    echo "PostgreSQL did not become ready" >&2
    false
fi

target_url="postgresql://odin@${postgres_container}:5432/odin"
maintenance_url="postgresql://odin_maintenance@${postgres_container}:5432/postgres"

step="candidate topology bootstrap"
docker run -d \
    --name "${api_container}" \
    --network "${network}" \
    -e DATABASE_URL="${target_url}" \
    -e DATABASE_MAINTENANCE_URL="${maintenance_url}" \
    -e DATABASE_PASSWORD_FILE=/run/secrets/postgres_app_password \
    -e DATABASE_MAINTENANCE_PASSWORD_FILE=/run/secrets/postgres_maintenance_password \
    -e ODIN_DB_BOOTSTRAP_OWNER=1 \
    -e ODIN_DB_ROLE=api \
    -e CORS_ORIGINS=http://school.test \
    -e TRUSTED_HOSTS=school.test,localhost \
    -e COOKIE_SECURE=false \
    --mount "type=bind,src=${app_password_file},dst=/run/secrets/postgres_app_password,readonly" \
    --mount "type=bind,src=${maintenance_password_file},dst=/run/secrets/postgres_maintenance_password,readonly" \
    --mount "type=volume,src=${application_volume},dst=/data" \
    "${image}" \
    /usr/bin/supervisord -n -c /etc/supervisor/conf.d/api.conf \
    >/dev/null

ready=0
for _attempt in $(seq 1 480); do
    if docker exec "${api_container}" curl -fsS http://localhost:8000/health/ready \
        >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 0.25
done
if [ "${ready}" != "1" ]; then
    echo "ODIN PostgreSQL API role did not become ready" >&2
    false
fi

step="candidate non-owner worker"
docker run -d \
    --name "${worker_container}" \
    --network "${network}" \
    -e DATABASE_URL="${target_url}" \
    -e DATABASE_PASSWORD_FILE=/run/secrets/postgres_app_password \
    -e ODIN_DB_BOOTSTRAP_OWNER=0 \
    -e ODIN_DB_ROLE=reports \
    -e ODIN_SCHEMA_WAIT_SECONDS=30 \
    --mount "type=bind,src=${app_password_file},dst=/run/secrets/postgres_app_password,readonly" \
    --mount "type=volume,src=${application_volume},dst=/data" \
    "${image}" \
    /usr/bin/supervisord -n -c /etc/supervisor/conf.d/reports.conf \
    >/dev/null
sleep 2
test "$(docker inspect --format '{{.State.Running}}' "${worker_container}")" = "true"

api_image_id="$(docker inspect --format '{{.Image}}' "${api_container}")"
worker_image_id="$(docker inspect --format '{{.Image}}' "${worker_container}")"
postgres_image_id="$(docker inspect --format '{{.Image}}' "${postgres_container}")"
if [ -z "${expected_image_id}" ]; then
    expected_image_id="$(docker image inspect --format '{{.Id}}' "${image}")"
fi
test "${api_image_id}" = "${expected_image_id}"
test "${worker_image_id}" = "${expected_image_id}"
test "${postgres_image_id}" = "$(docker image inspect --format '{{.Id}}' "${postgres_image}")"
printf 'database-parity-topology: {"api":"%s","worker":"%s","postgres_image_id":"%s","postgres_digest":"%s"}\n' \
    "${api_image_id}" "${worker_image_id}" "${postgres_image_id}" "${postgres_image#*@}"

docker rm -f "${worker_container}" "${api_container}" >/dev/null

step="legacy upgrade scenarios"
docker run --rm \
    --network "${network}" \
    --entrypoint python3 \
    --workdir /workspace \
    -e DATABASE_URL="${target_url}" \
    -e DATABASE_PASSWORD_FILE=/run/secrets/postgres_app_password \
    -e PYTHONPATH=/app/backend:/workspace \
    --mount "type=bind,src=${app_password_file},dst=/run/secrets/postgres_app_password,readonly" \
    --mount "type=bind,src=${PWD}/tests,dst=/workspace/tests,readonly" \
    --mount "type=bind,src=${PWD}/ops/database_parity/legacy_upgrade_drill.py,dst=/workspace/legacy_upgrade_drill.py,readonly" \
    "${image}" \
    /workspace/legacy_upgrade_drill.py

step="postgresql runtime workflow"
docker run --rm \
    --network "${network}" \
    --entrypoint /bin/bash \
    --workdir /workspace \
    -e DATABASE_URL="${target_url}" \
    -e DATABASE_MAINTENANCE_URL="${maintenance_url}" \
    -e DATABASE_PASSWORD_FILE=/run/secrets/postgres_app_password \
    -e DATABASE_MAINTENANCE_PASSWORD_FILE=/run/secrets/postgres_maintenance_password \
    -e ODIN_DB_ROLE=api \
    -e PYTHONPATH=/app/backend:/workspace \
    --mount "type=bind,src=${app_password_file},dst=/run/secrets/postgres_app_password,readonly" \
    --mount "type=bind,src=${maintenance_password_file},dst=/run/secrets/postgres_maintenance_password,readonly" \
    --mount "type=bind,src=${runtime_secret_file},dst=/run/secrets/runtime,readonly" \
    --mount "type=bind,src=${PWD}/ops/database_parity/runtime_probe.py,dst=/workspace/runtime_probe.py,readonly" \
    "${image}" \
    -c 'set -a; source /run/secrets/runtime; set +a; exec python3 /workspace/runtime_probe.py'

step="offline restore scenarios"
docker run --rm \
    --network "${network}" \
    --entrypoint python3 \
    --workdir /app/backend \
    -e DATABASE_URL="${target_url}" \
    -e DATABASE_MAINTENANCE_URL="${maintenance_url}" \
    -e DATABASE_PASSWORD_FILE=/run/secrets/postgres_app_password \
    -e DATABASE_MAINTENANCE_PASSWORD_FILE=/run/secrets/postgres_maintenance_password \
    -e PYTHONPATH=/app/backend \
    --mount "type=bind,src=${app_password_file},dst=/run/secrets/postgres_app_password,readonly" \
    --mount "type=bind,src=${maintenance_password_file},dst=/run/secrets/postgres_maintenance_password,readonly" \
    -v "${data_volume}:/data" \
    -v "$(pwd)/ops/database_parity/restore_drill.py:/tmp/restore_drill.py:ro" \
    "${image}" \
    /tmp/restore_drill.py

step="post-restore runtime restart"
restored_runtime="$(docker run --rm \
    --network "${network}" \
    --entrypoint /bin/bash \
    --workdir /workspace \
    -e DATABASE_URL="${target_url}" \
    -e DATABASE_PASSWORD_FILE=/run/secrets/postgres_app_password \
    -e ODIN_DB_ROLE=api \
    -e ODIN_PARITY_MODE=verify-restored \
    -e ODIN_EXPECTED_DATABASE_DIALECT=postgresql \
    -e PYTHONPATH=/app/backend:/workspace \
    --mount "type=bind,src=${app_password_file},dst=/run/secrets/postgres_app_password,readonly" \
    --mount "type=bind,src=${runtime_secret_file},dst=/run/secrets/runtime,readonly" \
    --mount "type=bind,src=${PWD}/ops/database_parity/runtime_probe.py,dst=/workspace/runtime_probe.py,readonly" \
    "${image}" \
    -c 'set -a; source /run/secrets/runtime; set +a; exec python3 /workspace/runtime_probe.py')"
printf '%s\n' "${restored_runtime}"
grep -Fq "postgresql-restored-runtime: PASS" <<<"${restored_runtime}"

if docker ps -a --format '{{.Names}}' | grep -Fx "${postgres_container}" >/dev/null; then
    docker rm -f "${postgres_container}" >/dev/null
fi
