#!/usr/bin/env bash
# Rebuild before testing so a stale local frontend cannot certify the pilot.
set -euo pipefail
pilot_repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$pilot_repo_root"
pilot_evidence_dir="${PILOT_EVIDENCE_DIR:-artifacts/edu-workflow-check}"
mkdir -p "$pilot_evidence_dir"
pilot_evidence_dir="$(cd "$pilot_evidence_dir" && pwd)"
(cd frontend && npm run build) > "$pilot_evidence_dir/education-browser-frontend-build.log" 2>&1
python3.11 -m pytest tests/test_contracts/test_education_connected_print_workflow.py tests/test_contracts/test_bambu_ftps_transfer.py -q --tb=short --junitxml="$pilot_evidence_dir/connected-classroom-tests.xml"
python3.11 -m pytest tests/education_browser/ -q --tb=short --junitxml="$pilot_evidence_dir/education-browser-tests.xml"
