# O.D.I.N. — Ops Scripts

Verification and controlled promotion tooling for the O.D.I.N. print farm management system.

> Release control is manual and owner-dispatched. Trusted Validation and Promotion
> Eligibility remain read-only. Immutable Image Publication builds once to a unique
> staging tag, tests both exact platform manifests, and then attaches immutable tags.
> Production Promotion moves `latest` to that tested digest without rebuilding.

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
make test-promotion-workflow
make test-mutation-workflows
make test-registry-contract

# Build/verify a sanitized, content-addressed bundle from a passing run
make practical-evidence SOURCE_RUN_DIR=artifacts/trusted-validation/<run-id> EXPECTED_SHA=<40hex>
make verify-practical-evidence EVIDENCE_DIR=artifacts/trusted-validation/<run-id>/evidence

# Version changes stay local until the explicit remote workflow sequence
make bump VERSION=1.9.13
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

## Authenticated promotion eligibility

`.github/workflows/promote.yml` is a read-only decision workflow for `stage`,
`demo`, or `production`. It downloads one exact Trusted Validation artifact,
verifies its canonical manifest digest, binds it to owner identity and immutable
workflow/run metadata, and uploads five decision files. Selecting an environment
creates GitHub approval/deployment audit metadata, but the workflow has no package,
repository-write, release, deployment, cluster, DNS, TLS, or secret mutation path.

The eligibility job does not check out the candidate. It fetches a fixed helper
closure from its own workflow SHA through the Contents API and verifies every
path, decoded byte count, and Git blob hash before importing from a fresh isolated
directory. It runs on the established M4 self-hosted runner; that runner is
trusted for owner-reviewed code but is not represented as VM-isolated.

## Immutable publication and production promotion

`.github/workflows/publish-image.yml` accepts an exact successful validation run
and `stage` eligibility run. It builds once to a run-unique staging tag, exercises
the amd64 and arm64 manifests by digest, then attaches `sha-<SHA>` and `vX.Y.Z`
only when absent or already equal. It never writes `latest`.

If a publication uploaded a schema-valid success receipt but GitHub marked the
run failed during later action cleanup, the same workflow can recover without a
rebuild. Supply the failed publication run ID and exact receipt SHA-256 together.
The workflow verifies the failed run and receipt binding, confirms both immutable
tags still resolve to the receipt digest, recreates only a run-unique staging
alias, and reruns both exact-platform health probes. Either recovery input by
itself, a non-failed run, or any digest/scope mismatch fails closed.

`.github/workflows/promote-production.yml` requires a fresh `production`
eligibility decision and exact successful publication receipt. It requires valid
public TLS and healthy production before mutation, creates and verifies a unique
rollback tag for the prior `latest` digest, and then copies the tested digest to
`latest` without source checkout or rebuild. Both mutation workflows serialize on
`odin-release-mutation`, isolate registry credentials, and upload HTML receipts.

The workflow receipt's rollback tag, digest, and registry-native restoration
command are the primary rollback path. Direct host access and semver compose
pinning are break-glass procedures only.
