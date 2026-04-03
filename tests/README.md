# O.D.I.N. Test Suite

## Quick Start

```bash
# On the server (your-odin-host)
cd /opt/odin

# Install dependencies
pip install -r tests/requirements-test.txt --break-system-packages

# Configure credentials
cp tests/.env.test.example tests/.env.test
# Edit tests/.env.test with your API key and admin credentials

# Run RBAC tests (the big one — security proof)
pytest tests/test_rbac.py -v --tb=short 2>&1 | tee rbac_results.txt

# Run security regression tests
pytest tests/test_security.py -v --tb=short 2>&1 | tee security_results.txt

# Run everything
pytest tests/ -v --html=test_report.html 2>&1 | tee full_results.txt

# Run the audit sweep
make test-audit

# Update visual baselines, then re-run the audit sweep
ODIN_UPDATE_VISUAL_BASELINE=1 pytest tests/test_e2e/test_audit_sweep.py -v --tb=short
```

## What Each File Tests

| File | Tests | Count | Purpose |
|------|-------|-------|---------|
| `conftest.py` | — | — | Shared fixtures, auth, test data |
| `test_rbac.py` | ~190 endpoints × 4 roles | ~770 | RBAC enforcement proof |
| `test_security.py` | Audit regression | ~65 | Verify security hardening holds |
| `test_e2e/test_audit_sweep.py` | Route crawl + synthetic printer telemetry + screenshots | varies | Broad UI audit |
| `test_contracts/test_api_contract_sweep.py` | OpenAPI read/invalid-payload/valid-create checks | varies | Backend contract sweep |

## Audit Sweep Notes

- The Playwright audit walk intentionally skips obviously destructive controls such as delete, stop, archive, and logout.
- Synthetic printer states are injected through the real MQTT monitor handler inside the running O.D.I.N. container, so the UI sees the same normalized telemetry path it would get from live printers.
- When the sweep is pointed at a remote ODIN instance from a different machine, stateful printer-simulation checks will auto-skip unless `ODIN_CONTAINER_EXEC` is configured to reach that same target container/pod.
- Visual regression is opt-in by baseline: if a baseline image is missing, the test skips until you create it with `ODIN_UPDATE_VISUAL_BASELINE=1`.
- Audit artifacts are written under `tests/artifacts/audit/`:
  - `e2e/routes/*.json` for route crawl diagnostics
  - `e2e/printer-states/*.json` for state-surface results
  - `e2e/visual/*.json` plus `tests/artifacts/visual/` images for visual diffs
  - `contracts/*.json` for read/write/create endpoint results

## Test Users

The suite auto-creates 3 test users (viewer, operator, admin) and deletes them after.
You only need to provide the REAL admin credentials in `.env.test`.

## Interpreting Results

- **RBAC BYPASS** = CRITICAL: an endpoint allowed access it shouldn't have
- **RBAC OVER-RESTRICTED** = a role was denied access it should have
- **All passed** = authorization enforcement is working correctly

## Environment Variables (alternative to .env.test)

```bash
export ODIN_BASE_URL=http://localhost:8000
export ODIN_API_KEY=your-api-key
export ODIN_ADMIN_USERNAME=admin
export ODIN_ADMIN_PASSWORD=YourPassword
```
