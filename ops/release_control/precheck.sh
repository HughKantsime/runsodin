#!/usr/bin/env bash
# Run the Trusted Validation components that most often fail, before pushing a
# release candidate: CV01 release control, CV03 security and CV02 contracts, in
# sequence (parallel load makes timing-sensitive contracts flaky). Set
# PRECHECK_FULL=1 to add CV04 candidate image and CV05 database parity, for
# releases that add migrations or change the image. It doesn't replace Trusted Validation,
# which still runs all ten components; it means the real run normally passes
# the first time. Evidence goes to artifacts/precheck/<timestamp>/.
set -euo pipefail
cd "$(dirname "$0")/../.."
PYTHON="${PYTHON:-python3.11}"
# Components run under an isolated HOME; point browser tests at the real cache, as aggregate.py does.
export PLAYWRIGHT_BROWSERS_PATH="$("$PYTHON" -c 'import os,sys; from pathlib import Path; print(os.getenv("PLAYWRIGHT_BROWSERS_PATH") or str((Path.home()/"Library/Caches" if sys.platform == "darwin" else Path(os.getenv("XDG_CACHE_HOME", str(Path.home()/".cache"))))/"ms-playwright"))')"
# Likewise Docker: the isolated HOME has no Docker context, so PostgreSQL tests would skip.
export DOCKER_HOST="${DOCKER_HOST:-$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null)}"
stamp="$(date -u +%Y%m%dt%H%M%Sz)"
out="artifacts/precheck/$stamp"
mkdir -p "$out"

if [ ! -x .venv-contracts/bin/python ]; then
  "$PYTHON" -m venv .venv-contracts
  .venv-contracts/bin/pip install --disable-pip-version-check -q -r tests/requirements-contracts.txt
fi
[ -d frontend/node_modules ] || npm --prefix frontend ci --silent --no-audit --no-fund

run() {  # run <name> <command...>; record pass/fail without stopping the others
  local name="$1"; shift
  local started=$SECONDS
  if "$@" >"$out/$name.log" 2>&1; then
    echo "PASS $name ($((SECONDS - started))s)" | tee -a "$out/summary.txt"
  else
    echo "FAIL $name ($((SECONDS - started))s): see $out/$name.log" | tee -a "$out/summary.txt"
    return 1
  fi
}

status=0
run cv01-release-control make release-control-local-gate PYTHON="$PYTHON" || status=1
run cv03-security make security-structured PYTHON="$PYTHON" SECURITY_PYTHON="$PYTHON" || status=1
run cv02-contracts make test-contracts-structured PYTHON="$PYTHON" || status=1
if [ "${PRECHECK_FULL:-0}" = 1 ]; then
  run cv04-candidate make test-candidate CANDIDATE_PYTHON="$PYTHON" || status=1
  run cv05-database-parity make test-database-parity CANDIDATE_PYTHON="$PYTHON" || status=1
fi

if [ "$status" -ne 0 ]; then
  echo "release-precheck FAILED; fix before pushing a candidate ($out/summary.txt)"
else
  echo "release-precheck passed ($out/summary.txt)"
fi
exit "$status"
