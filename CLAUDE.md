# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

O.D.I.N. (Orchestrated Dispatch & Inventory Network) is a self-hosted 3D print farm management system. Single Docker container running 7 supervised processes: FastAPI backend, 4 printer monitor daemons (Bambu MQTT, Moonraker, PrusaLink, Elegoo), go2rtc camera streaming, and vision_monitor AI failure detection. Python 3.11 backend, React 18 frontend, SQLite (WAL mode) database.

## Build & Run Commands

Prefer `make` targets over raw commands. Run `make help` for the full list.

```bash
make build                  # docker compose up -d --build
make test                   # Phase 0 + pytest (skip build)
make deploy                 # full pipeline: build + Phase 0 + pytest
make bump VERSION=X.Y.Z    # bump + commit + tag (no push)
make release VERSION=X.Y.Z # bump + commit + tag + push
make logs                   # tail container logs
make shell                  # bash into container
```

The full pipeline runs locally on Mac via Docker Desktop — no sandbox SSH required.

```bash
# Frontend dev server (port 5173, proxies API to localhost:8000)
cd frontend && npm install && npm run dev

# Backend standalone
cd backend && pip install -r requirements.txt && uvicorn main:app --reload
```

## Testing

Tests run against a live API instance (no mocking). The container must be running.

```bash
pip install -r tests/requirements-test.txt
make test                                      # full pipeline (recommended)
pytest tests/ -v --tb=short                    # all tests (manual)
pytest tests/test_rbac.py -v                   # RBAC enforcement (~500 tests)
pytest tests/test_security.py -v               # security regression tests
pytest tests/test_rbac.py::test_name -v        # single test
pytest tests/ -v --html=test_report.html       # with HTML report
```

RBAC tests must run separately from other tests (collection conflicts). `make test` and `deploy_local.sh` handle this automatically.

Test config via `tests/.env.test` (copy from `.env.test.example`) or environment variables: `BASE_URL`, `API_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`. `ADMIN_PASSWORD` is required — tests will fail if not set.

## Version Bumping

```bash
make bump VERSION=1.0.XX          # bump + commit + tag (no push)
make release VERSION=1.0.XX       # bump + commit + tag + push
```

Updates: `VERSION`, `frontend/package.json`, `backend/main.py` (`__version__`), `docker-compose.yml` image tag, `install/install.sh`. GHCR workflow triggers on tag push. Works on both macOS and Linux.

## Deployment

```bash
# Local (Mac) — builds from source + Phase 0 + pytest
make deploy                      # full pipeline (recommended)
./ops/deploy_local.sh            # full pipeline
./ops/deploy_local.sh --skip-build  # retest only
./ops/deploy_local.sh --skip-tests  # Phase 0 only

# Sandbox (.200) — optional, for hardware staging
./ops/deploy_sandbox.sh              # full pipeline
./ops/deploy_sandbox.sh --skip-build # retest only

# Production (.211) — pulls from GHCR, never builds
./ops/deploy_prod.sh                 # deploy :latest
./ops/deploy_prod.sh v1.0.XX         # deploy specific tag
./ops/deploy_prod.sh --check-only    # Phase 0 health check only

# Health verification (runs automatically in deploy scripts)
./ops/phase0_verify.sh               # auto-detect environment
./ops/phase0_verify.sh local         # force local mode
```

Production compose: `/opt/odin/runsodin/runsodin/docker-compose.yml`. Production deploys are logged to `/opt/odin/deploy.log`.

## Architecture

### Backend (`backend/`)

- **main.py** — FastAPI app entry point (lifespan, middleware, CORS). Routes split into 13 modules under `routers/`.
- **models.py** — SQLAlchemy ORM: Printer, Job, Model, Spool, FilamentSlot, etc.
- **schemas.py** — Pydantic request/response models
- **auth.py** — PyJWT (HS256, 24h expiry) + bcrypt password hashing
- **scheduler.py** — Job scheduling engine: color-match scoring, time slot allocation, blackout hours, load balancing
- **crypto.py** — Fernet encryption for printer credentials at rest
- **printer_adapter.py** — Base adapter interface; `bambu_adapter.py`, `moonraker_adapter.py`, `prusalink_adapter.py`, `elegoo_adapter.py` implement it
- **printer_events.py** → **alert_dispatcher.py** — Observer pattern: state changes dispatch to push notifications, email, Discord/Slack webhooks, ntfy, Telegram. Resolves printer `org_id` for org-level quiet hours and org webhook dispatch.
- **quiet_hours.py** — Notification suppression during configured quiet hours (system-level and org-level). Digest generation at quiet period end.
- **ws_hub.py** — WebSocket event broadcasting to connected clients
- **license_manager.py** — Air-gap Ed25519 license validation (Community/Pro/Education/Enterprise tiers)
- **vision_monitor.py** — AI print failure detection daemon (spaghetti, first layer, detachment). ONNX inference on go2rtc camera frames. Per-printer threads, confirmation buffers, auto-pause.

### Monitor Daemons

Each runs as a separate supervisord process:
- **mqtt_monitor.py** — Bambu Lab MQTT telemetry (subscribes to `device/{serial}/report`)
- **moonraker_monitor.py** — Klipper REST polling (3-sec interval)
- **prusalink_monitor.py** — Prusa HTTP polling
- **elegoo_monitor.py** — Elegoo SDCP protocol
- **vision_monitor.py** — Camera-based AI failure detection (priority 35, after go2rtc)

### Frontend (`frontend/src/`)

- **App.jsx** — React Router layout + sidebar
- **api.js** — Fetch-based API client (`fetchAPI` wrapper) with all endpoint definitions
- **permissions.js** — Client-side RBAC permission checker
- **pages/** — ~20 page components (Dashboard, Printers, Jobs, Models, Orders, Spools, Timeline, Analytics, Settings, Detections, etc.)
- **hooks/useWebSocket.js** — Real-time state updates from backend
- **contexts/** — BrandingContext, LicenseContext

Stack: React 18, Vite 5, TailwindCSS 3, React Query 5, React Router 6, Recharts, Lucide icons. No TypeScript — plain JSX.

### Docker (`docker/`)

- **supervisord.conf** — Manages all 7 processes with auto-restart
- **entrypoint.sh** — Auto-generates secrets, initializes DB (WAL mode), creates tables
- **go2rtc.yaml** — Camera streaming relay config

### Data Flow

1. Printer monitors poll/subscribe and update SQLite on state changes
2. State changes trigger `printer_events.dispatch()` → alerts + WebSocket push
3. Frontend receives real-time updates via WebSocket, falls back to HTTP polling
4. Scheduler assigns pending jobs to printers by color-match score and availability

### Vigil AI (`backend/vision_monitor.py`)

- DB tables: `vision_detections`, `vision_settings`, `vision_models` (created in `entrypoint.sh`, not SQLAlchemy)
- Frames stored at `/data/vision_frames/{printer_id}/`, served via `/api/vision/frames/`
- Models stored at `/data/vision_models/`, defaults copied from `backend/vision_models_default/` on first boot
- Default thresholds duplicated in vision_monitor.py, main.py API defaults, and SQL schema — keep in sync
- ONNX models tracked via **Git LFS** (`.gitattributes` tracks `*.onnx`) — GitHub rejects files >100 MB without LFS

## Key Conventions

- **Auth model**: Three tiers — no headers (blocked), API key only (perimeter), JWT+API key (full RBAC with viewer/operator/admin roles)
- **API prefix**: All routes under `/api/`. Swagger at `/api/docs`, ReDoc at `/api/redoc`
- **Database**: SQLite at `/data/odin.db`. Several tables created via raw SQL in `entrypoint.sh` (not in SQLAlchemy `models.py`): `users`, `groups`, `print_jobs`, `print_files`, `oidc_config`, `webhooks`, `vision_detections`, `vision_settings`, `vision_models`, `api_tokens`, `active_sessions`, `token_blacklist`, `quota_usage`, `model_revisions`, `report_schedules`, `timelapses`
- **Org settings**: Per-org config stored as JSON in `groups.settings_json`. Keys: `default_filament_type`, `default_filament_color`, `quiet_hours_*`, `webhook_url`, `webhook_type`, `branding_app_name`, `branding_logo_url`. Helper: `routers/orgs.py:_get_org_settings()` merges with `DEFAULT_ORG_SETTINGS`.
- **Secrets**: Auto-generated on first run, persisted in `/data/`. `ENCRYPTION_KEY` (Fernet), `JWT_SECRET_KEY`, `API_KEY`
- **Pre-commit**: gitleaks for secret scanning
- **Git LFS**: Required for binary assets >100 MB (ONNX models). `*.onnx` tracked in `.gitattributes`
- **Version sources**: `VERSION` file is source of truth; also in `frontend/package.json`, `backend/main.py __version__`, `docker-compose.yml` image tag
- **License**: BSL 1.1 (converts to Apache 2.0 on 2029-02-07). Cannot offer as hosted service to third parties.
- **Commit style**: `feat:`, `fix:`, `refactor:`, `release:` prefixes. No co-author lines.
- **Adding API endpoints**: Backend route in `main.py` → frontend function in `api.js` using `fetchAPI` wrapper (not Axios). Always add both sides.
- **Code style**: No strict linter. Python follows existing patterns in `main.py`. React uses functional components with hooks + TailwindCSS (no CSS modules).

## Server Topology

- **Local (Mac)**: Primary dev/test/release environment (Docker Desktop)
- **Sandbox**: `.200` — optional hardware staging instance
- **Production**: `.211` — live instance (pulls from GHCR)
- **License Manager**: `.6` — issues and verifies Ed25519 licenses (`/opt/odin-license-manager/`)

When debugging cross-server issues, always confirm which server a request originates from and which it targets. Trace the full request path before suggesting fixes.

## Release Workflow

Release pipeline runs locally on Mac: `make deploy` (build → Phase 0 → pytest) → `make release VERSION=X.Y.Z` (bump → push → GHCR build). Prod (.211) pulls released images manually.

The sandbox (.200) is no longer required for releases but remains available for hardware staging (e.g., testing with physical printers).

When deploying or completing a feature, ALWAYS bump the version and create a git tag. Never suggest skipping version bumps — if code is going live, it gets a new version.

## Seed Data & Demo Scripts

When creating seed data or demo data, validate against actual model schemas and enum definitions in `models.py` and `schemas.py` BEFORE writing the script. Check enum case sensitivity, field types (dict vs list), and ensure no references to non-existent resources (e.g., fake printer IPs that monitors will try to reach). Prefer minimal, realistic seed data over comprehensive fake data.
