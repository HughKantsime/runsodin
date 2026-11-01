#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
browser_python="${EDU_BROWSER_PYTHON:-python3}"
frontend_url="${EDU_FRONTEND_URL:-http://127.0.0.1:4173}"

cd "$repo_root"
ADMIN_USERNAME=ci ADMIN_PASSWORD=ci EDU_FRONTEND_URL="$frontend_url" \
  "$browser_python" ops/edu_readiness/preview_server.py --url "$frontend_url" -- \
  "$browser_python" ops/demo/run_junit_gate.py -- \
  "$browser_python" -m pytest tests/edu_browser/test_edu_ui.py -v --tb=short \
  -o xfail_strict=true --junitxml="{junit}"
