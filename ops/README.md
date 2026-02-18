# O.D.I.N. — Ops Scripts

Verification and release automation for the O.D.I.N. print farm management system.

## Scripts

### `phase0_verify.sh` — Health Check Gate

Single-command verification that a deployment is healthy. Runs on local, sandbox, and prod.

```bash
./ops/phase0_verify.sh              # auto-detect environment
./ops/phase0_verify.sh local        # force local mode
./ops/phase0_verify.sh prod         # force production mode
```

**What it checks:**

| Phase | What | Fails if |
|-------|------|----------|
| 0A — Provenance | Compose file, image source, VERSION | Prod has `build:`, wrong GHCR image |
| 0B — Process Health | Container status, healthcheck, supervisor (all 6 services) | Any service not RUNNING, crash loop |
| 0C — API Sanity | /health, /api/config, /api/printers, /api/jobs | Any 500 or connection refused |
| 0D — Configuration | ENCRYPTION_KEY, JWT_SECRET_KEY, DATABASE_URL | Any env var missing or empty |
| 0E — Prod Guardrail | No `build:` in compose, image tag check | Active `build:` directive on prod |
| 0F — Auth Smoke | Login → get JWT → hit /api/auth/me | Login fails, token invalid |
| 0G — DB Write Probe | Create backup → read back | DB write or read fails |

**Auth smoke (0F/0G) requires credentials:**

```bash
export ODIN_ADMIN_PASSWORD="YourAdminPassword"
./ops/phase0_verify.sh
```

**Exit codes:** 0 = passed, 1 = failed.

---

### `bump-version.sh` — Version Bump + Tag

Bumps version across all files, commits, and tags.

```bash
./ops/bump-version.sh 1.3.46          # bump + commit + tag (no push)
./ops/bump-version.sh 1.3.46 --push   # bump + commit + tag + push
./ops/bump-version.sh                  # show current version
```

**Files updated:** `VERSION`, `frontend/package.json`, `backend/main.py`, `docker-compose.yml`, `install/install.sh`, `frontend/public/sw.js`

---

### `seed_demo_full.py` — Demo Data Seeder

Seeds realistic demo data via the API for testing and demos.

---

## Quick Reference

```bash
# Build and test locally
make build                          # docker compose up -d --build
make verify                         # Phase 0 health checks
make test                           # main + RBAC tests

# Release
make release VERSION=1.3.46        # bump + commit + tag + push
# GHCR workflow triggers on tag push

# Production (manual, as end user)
ssh root@192.168.71.211
cd /opt/odin/runsodin/runsodin
docker compose pull && docker compose up -d
```
