#!/usr/bin/env bash
# Public, no-secret production readiness gate for GitHub Actions.
#
# Checks:
#   1. /health reports the expected deployed version.
#
# This intentionally does not run protected readiness, host-local Docker,
# supervisor, auth, or DB write probes. Keep those in ops/phase0_verify.sh
# for approved operator runs.

set -euo pipefail

BASE_URL="${ODIN_PROD_BASE_URL:-https://odin.subsystem.app}"
EXPECTED_VERSION="${1:-${ODIN_EXPECTED_VERSION:-}}"
MAX_ATTEMPTS="${ODIN_PROD_VERIFY_ATTEMPTS:-20}"
SLEEP_SECONDS="${ODIN_PROD_VERIFY_SLEEP_SECONDS:-30}"

if [[ -z "$EXPECTED_VERSION" ]]; then
  echo "usage: $0 <expected-version>" >&2
  echo "or set ODIN_EXPECTED_VERSION" >&2
  exit 2
fi

EXPECTED_VERSION="${EXPECTED_VERSION#v}"
BASE_URL="${BASE_URL%/}"

HEALTH_URL="${BASE_URL}/health"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

last_health_status=""
last_health_version=""
last_error=""

fetch_json() {
  local url="$1"
  local out_file="$2"
  local status

  status="$(curl -fsS -m 10 -w '%{http_code}' -o "$out_file" "$url" 2>"$out_file.err" || true)"
  if [[ "$status" != "200" ]]; then
    last_error="$(<"$out_file.err")"
  fi
  printf '%s' "$status"
}

json_field() {
  local file="$1"
  local field="$2"

  python3 - "$file" "$field" <<'PY'
import json
import sys

path, field = sys.argv[1:3]
try:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
except Exception:
    sys.exit(0)

value = payload.get(field)
if isinstance(value, bool):
    print("true" if value else "false")
elif value is not None:
    print(value)
PY
}

write_summary() {
  if [[ -z "${GITHUB_STEP_SUMMARY:-}" ]]; then
    return
  fi

  {
    echo "## ODIN Production Verification"
    echo
    echo "- Expected version: \`$EXPECTED_VERSION\`"
    echo "- Base URL: \`$BASE_URL\`"
    echo "- Health endpoint: \`$HEALTH_URL\`"
    echo "- Last health status: \`${last_health_status:-n/a}\`"
    echo "- Last health version: \`${last_health_version:-n/a}\`"
  } >> "$GITHUB_STEP_SUMMARY"
}

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  health_json="$tmp_dir/health.json"

  last_health_status="$(fetch_json "$HEALTH_URL" "$health_json")"
  if [[ "$last_health_status" == "200" ]]; then
    last_health_version="$(json_field "$health_json" version)"
  else
    last_health_version=""
  fi

  if [[ "$last_health_status" != "200" || "$last_health_version" != "$EXPECTED_VERSION" ]]; then
    echo "Poll $attempt/$MAX_ATTEMPTS: waiting for /health version $EXPECTED_VERSION; got status ${last_health_status:-none}, version ${last_health_version:-none}"
    sleep "$SLEEP_SECONDS"
    continue
  fi

  echo "Prod is running v$EXPECTED_VERSION"
  write_summary
  exit 0
done

write_summary
echo "::error::Prod did not serve /health with v$EXPECTED_VERSION on $BASE_URL within $((MAX_ATTEMPTS * SLEEP_SECONDS)) seconds"
if [[ -n "$last_error" ]]; then
  echo "::error::Last curl error: $last_error"
fi
exit 1
