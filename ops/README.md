# O.D.I.N. — Ops Scripts

Verification and controlled promotion tooling for the O.D.I.N. print farm management system.

> Release-control migration (local foundation): ordinary pushes and pull
> requests no longer run ODIN repository workflows. Publication and production
> promotion remain disabled until the later reviewed workflows are installed.

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
export ODIN_ADMIN_PASSWORD="<your-password>"
./ops/phase0_verify.sh
```

**Exit codes:** 0 = passed, 1 = failed.

---

### `bump-version.sh` — Local Version Commit

Bumps version across all files and creates one local commit. It never tags,
pushes, publishes, or deploys.

```bash
./ops/bump-version.sh 1.9.13          # update files + local commit
./ops/bump-version.sh                 # show current version
```

**Files updated:** `VERSION`, frontend package metadata,
`backend/core/app.py`, Compose/install version references, service-worker cache,
and generated design tokens.

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

# Candidate validation (manual trusted workflow after remote bootstrap)
make trusted-validation-gate

# Build/verify a sanitized, content-addressed bundle from a passing run
make practical-evidence SOURCE_RUN_DIR=artifacts/trusted-validation/<run-id> EXPECTED_SHA=<40hex>
make verify-practical-evidence EVIDENCE_DIR=artifacts/trusted-validation/<run-id>/evidence

# Local release entry point is deliberately disabled
make release                       # fails with promotion-workflow guidance
```

## Practical release evidence

`make practical-evidence` accepts one completed 10/10 trusted-validation run.
It validates every component digest, reconciles source identity across candidate,
database, EDU sandbox, and hardware manifests, and writes canonical
`manifest.json`, `manifest.sha256`, and a human-readable `index.html`.

The passing GitHub workflow uploads only that `evidence/` directory. Raw logs and
JUnit files are hashed into its inventory but are not included in the promotion
artifact. The bundle proves validation integrity; it does not authorize a deploy.
Every stage, demo, or production action still requires a separate owner-dispatched
promotion workflow. Production additionally requires its protected GitHub
environment approval.
