# O.D.I.N. — Ops Scripts

Deployment verification and release automation for the O.D.I.N. print farm management system.

## Scripts

### `phase0_verify.sh` — Deployment Gate

Single-command verification that a deployment is healthy. Runs on both sandbox and prod.

```bash
# Auto-detect environment
./ops/phase0_verify.sh

# Force environment
./ops/phase0_verify.sh sandbox
./ops/phase0_verify.sh prod
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
| 0G — DB Write Probe | Create spool → read back → delete | DB write or read fails |

**Auth smoke (0F/0G) requires credentials:**

```bash
# Set before running, or export in your shell profile
export ODIN_ADMIN_USER="admin"          # defaults to "admin" if not set
export ODIN_ADMIN_PASSWORD="YourAdminPassword"
./ops/phase0_verify.sh prod
```

Without credentials, 0F/0G will show warnings but won't fail the gate.

**Exit codes:** 0 = passed, 1 = failed.

---

### `deploy_sandbox.sh` — Sandbox Build + Test

Builds from source, runs Phase 0, then runs the pytest suite (Phases 1-3).

```bash
./ops/deploy_sandbox.sh              # full pipeline
./ops/deploy_sandbox.sh --skip-build # retest only
./ops/deploy_sandbox.sh --skip-tests # Phase 0 only
```

**Location:** Runs on sandbox (.70.200) at `/opt/printfarm-scheduler/`

---

### `deploy_prod.sh` — Production Deploy (Pull Only)

Pulls from GHCR, restarts container, runs Phase 0. **Never builds.**

```bash
./ops/deploy_prod.sh                 # deploy :latest
./ops/deploy_prod.sh v1.0.21        # deploy specific tag
./ops/deploy_prod.sh --check-only   # Phase 0 without deploying
```

**Location:** Runs on prod (.71.211) at `/opt/odin/`

**Features:**
- Pre-flight check aborts if `build:` exists in compose
- Swaps image tag in compose when pinning a version
- Logs every deploy to `/opt/odin/deploy.log` (timestamp, tag, version, digest)

---

### `RELEASE_CHECKLIST.md` — Step-by-Step Release Process

Copy-paste checklist covering pre-release → tag → deploy → verify → rollback.

---

## File Locations

| Server | Path | Scripts |
|--------|------|---------|
| Sandbox (.70.200) | `/opt/printfarm-scheduler/ops/` | All 4 files |
| Prod (.71.211) | `/opt/odin/ops/` | All 4 files |

## Environment Files

| Server | .env location |
|--------|--------------|
| Sandbox | `/opt/printfarm-scheduler/.env` |
| Prod | `/opt/odin/runsodin/runsodin/.env` |

Required env vars: `ENCRYPTION_KEY`, `JWT_SECRET_KEY`, `DATABASE_URL`, `ODIN_HOST_IP`, `TZ`

## Quick Reference

```bash
# Full release workflow
# 1. On sandbox: build + test
ssh root@192.168.70.200
cd /opt/printfarm-scheduler && ./ops/deploy_sandbox.sh

# 2. Tag + push (from dev machine)
git tag v1.0.XX && git push origin main v1.0.XX

# 3. On prod: deploy
ssh root@192.168.71.211
cd /opt/odin && ./ops/deploy_prod.sh v1.0.XX
```
