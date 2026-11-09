#!/usr/bin/env bash
# Shared by trusted-validation Actions and the SSH VM precheck.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo"
python3.11 -c 'import sys; sys.version_info[:2] == (3, 11) or sys.exit("Python 3.11 is required")'

# Include transitive requirements: contracts includes backend/requirements.txt.
# Hash this script too so changes to install commands invalidate existing stamps.
fingerprint() {
  sha256sum ops/release_control/bootstrap_validation_deps.sh "$@" | sha256sum | cut -d ' ' -f 1
}
bootstrap_venv() {
  local venv="$1" stamp digest
  shift
  stamp="$venv/.validation-deps.sha256"
  digest="$(fingerprint "$@")"
  if [[ ! -x "$venv/bin/python" || ! -x "$venv/bin/pip" || ! -f "$stamp" || "$(cat "$stamp")" != "$digest" ]]; then
    rm -f "${stamp:?}"
    python3.11 -m venv "$venv"
    local args=() requirement
    for requirement in "$@"; do args+=(-r "$requirement"); done
    "$venv/bin/pip" install --disable-pip-version-check "${args[@]}"
    printf '%s\n' "$digest" > "$stamp"
  fi
}
bootstrap_venv .hardware-cert-venv backend/requirements.txt ops/hardware_certification/requirements.txt
bootstrap_venv .venv-stress backend/requirements.txt
bootstrap_venv .venv-contracts tests/requirements-contracts.txt backend/requirements.txt

frontend_stamp=frontend/node_modules/.validation-deps.sha256
frontend_digest="$(fingerprint frontend/package.json frontend/package-lock.json)"
if [[ ! -d frontend/node_modules || ! -f "$frontend_stamp" || "$(cat "$frontend_stamp")" != "$frontend_digest" ]]; then
  rm -f "${frontend_stamp:?}"
  npm --prefix frontend ci
  printf '%s\n' "$frontend_digest" > "$frontend_stamp"
fi
# Playwright itself checks its browser cache. Always call the installers so a
# removed/relocated cache is repaired even when dependency stamps still match.
PLAYWRIGHT_SKIP_BROWSER_GC=1 .venv-contracts/bin/python -m playwright install chromium
(cd frontend && PLAYWRIGHT_SKIP_BROWSER_GC=1 npx --no-install playwright install chromium)
