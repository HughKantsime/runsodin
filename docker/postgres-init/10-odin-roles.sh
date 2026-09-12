#!/bin/bash
set -Eeuo pipefail

read_secret() {
    local path="$1"
    local label="$2"
    local mode
    if [[ ! -f "${path}" ]]; then
        printf '%s secret file is missing\n' "${label}" >&2
        return 1
    fi
    mode="$(stat -c '%a' "${path}")"
    if [[ "${mode}" != "400" && "${mode}" != "600" ]]; then
        printf '%s secret file must have mode 0400 or 0600\n' "${label}" >&2
        return 1
    fi
    local value
    value="$(<"${path}")"
    if [[ -z "${value}" || "${value}" == *$'\n'* || "${value}" == *$'\r'* ]]; then
        printf '%s secret file is invalid\n' "${label}" >&2
        return 1
    fi
    printf '%s' "${value}"
}

export ODIN_INIT_APP_PASSWORD
export ODIN_INIT_MAINTENANCE_PASSWORD
ODIN_INIT_APP_PASSWORD="$(read_secret "${ODIN_APP_PASSWORD_FILE:?}" "application")"
ODIN_INIT_MAINTENANCE_PASSWORD="$(
    read_secret "${ODIN_MAINTENANCE_PASSWORD_FILE:?}" "maintenance"
)"

psql --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<'SQL'
\set ON_ERROR_STOP on
\getenv app_password ODIN_INIT_APP_PASSWORD
\getenv maintenance_password ODIN_INIT_MAINTENANCE_PASSWORD
CREATE ROLE odin
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
    PASSWORD :'app_password';
CREATE ROLE odin_maintenance
    LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
    PASSWORD :'maintenance_password';
ALTER DATABASE odin OWNER TO odin;
ALTER SCHEMA public OWNER TO odin;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO odin;
SQL

unset ODIN_INIT_APP_PASSWORD ODIN_INIT_MAINTENANCE_PASSWORD
