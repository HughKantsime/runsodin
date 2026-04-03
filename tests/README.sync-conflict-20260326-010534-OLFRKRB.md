# O.D.I.N. Test Suite

## Quick Start

```bash
# On the server (your-odin-host)
cd /opt/printfarm-scheduler

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
```

## What Each File Tests

| File | Tests | Count | Purpose |
|------|-------|-------|---------|
| `conftest.py` | — | — | Shared fixtures, auth, test data |
| `test_rbac.py` | 126 endpoints × 4 roles | ~500 | RBAC enforcement proof |
| `test_security.py` | Audit regression | ~35 | Verify v1.0.0 fixes hold |

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
