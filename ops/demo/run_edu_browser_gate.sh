#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
frontend_dir="$repo_root/frontend"
browser_python="${EDU_BROWSER_PYTHON:-python3}"
preview_log="$(mktemp -t odin-edu-preview.XXXXXX)"

cleanup() {
  if [[ -n "${preview_pid:-}" ]]; then
    kill "$preview_pid" 2>/dev/null || true
    wait "$preview_pid" 2>/dev/null || true
  fi
  rm -f "$preview_log"
}
trap cleanup EXIT

cd "$frontend_dir"
npm run preview -- --host 127.0.0.1 --port 4173 >"$preview_log" 2>&1 &
preview_pid=$!

for _ in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:4173/ >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$preview_pid" 2>/dev/null; then
    cat "$preview_log" >&2
    exit 1
  fi
  sleep 0.25
done
curl -fsS http://127.0.0.1:4173/ >/dev/null

cd "$repo_root"
ADMIN_USERNAME=ci ADMIN_PASSWORD=ci EDU_FRONTEND_URL=http://127.0.0.1:4173 \
  "$browser_python" ops/demo/run_junit_gate.py -- \
  "$browser_python" -m pytest tests/edu_browser/test_edu_ui.py -v --tb=short \
  -o xfail_strict=true --junitxml={junit}
