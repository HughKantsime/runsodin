# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Section 1: Portfolio Context

```
PRODUCT:        O.D.I.N. (Orchestrated Dispatch & Inventory Network)
TAGLINE:        One Dashboard for Every Printer.
VERSION:        v1.3.45
STATUS:         Built, pre-launch
TIER:           1: Launch Now
PRIORITY:       Commercial launch — legal docs, pricing page, Reddit/GitHub GTM execution
BLOCKERS:       Legal docs (ToS, Privacy, EULA, Vigil AI disclaimer) — DONE. Remaining: publish pricing on runsodin.com, Discord community, Reddit launch posts
MASTER DOC:     ../MASTER_PORTFOLIO.md
```

### Tier Definitions

- **Tier 1 — Launch Now:** Revenue-blocking. Active development + GTM execution. Full effort.
- **Tier 2 — Ready When Needed:** Built but parked. Only work on this when a Tier 1 product has predictable revenue. Bug fixes and minor improvements only. No new features.
- **Tier 3 — Concept → Build:** Do not build until trigger conditions are met. Planning and research only.

### Accountability Rules

- If Hugh asks for a new feature that isn't on the blocker list, flag it: "This is new scope. Your launch blockers are still [X, Y, Z]. Want to address those first?"
- Never gold-plate. Ship working solutions, not perfect ones.
- If Hugh has been coding for 3+ hours without touching GTM, distribution, legal, or docs — say something.

---

## Section 2: Technical Context

```
STACK:          Python 3.11, FastAPI, React 18, SQLite (WAL), Docker, supervisord
REPO:           github.com/HughKantsime/runsodin
ENTRY POINT:    make build (container) or make test (pytest)
TEST COMMAND:   make test
TEST COUNT:     ~965 (130 main + 770 RBAC + 65 security/e2e)
DEPLOY:         Docker single-container. Local Mac builds → GHCR → prod pulls.
```

### What It Is

Self-hosted 3D print farm management platform. Single Docker container running 7 supervised processes: FastAPI backend, 4 printer monitor daemons (Bambu MQTT, Moonraker/Klipper, PrusaLink, Elegoo SDCP), go2rtc camera streaming, and Vigil AI print failure detection (ONNX). Python 3.11 backend, React 18 frontend, SQLite (WAL mode) database.

### Build & Run Commands

Prefer `make` targets over raw commands. Run `make help` for the full list.

```bash
make build                  # docker compose up -d --build
make test                   # main + RBAC pytest suites
make test-security          # Layer 3 adversarial security tests
make test-e2e               # E2E Playwright tests
make verify                 # Phase 0 health checks
make bump VERSION=X.Y.Z    # bump + commit + tag (no push)
make release VERSION=X.Y.Z # bump + commit + tag + push
make logs                   # tail container logs
make shell                  # bash into container
```

```bash
# Frontend dev server (port 5173, proxies API to localhost:8000)
cd frontend && npm install && npm run dev

# Backend standalone
cd backend && pip install -r requirements.txt && uvicorn main:app --reload
```

### Testing

Tests run against a live API instance (no mocking). The container must be running.

```bash
pip install -r tests/requirements-test.txt
make test                                      # main + RBAC (recommended)
make test-security                             # Layer 3 adversarial security
make test-e2e                                  # E2E Playwright (requires browser)
pytest tests/test_rbac.py -v                   # RBAC enforcement (~770 tests)
pytest tests/test_security.py -v               # security regression tests
pytest tests/security/ -v                      # adversarial security tests
pytest tests/test_rbac.py::test_name -v        # single test
pytest tests/ -v --html=test_report.html       # with HTML report
```

RBAC tests must run separately from other tests (collection conflicts). `make test` handles this automatically.

Test config via `tests/.env.test` (copy from `.env.test.example`) or environment variables: `BASE_URL`, `API_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`. `ADMIN_PASSWORD` is required — tests will fail if not set.

### Version Bumping

```bash
make bump VERSION=1.0.XX          # bump + commit + tag (no push)
make release VERSION=1.0.XX       # bump + commit + tag + push
```

Updates: `VERSION`, `frontend/package.json`, `backend/main.py` (`__version__`), `docker-compose.yml` image tag, `install/install.sh`. GHCR workflow triggers on tag push. Works on both macOS and Linux.

### Deployment

Local dev builds via Docker Desktop. Production pulls from GHCR manually (as an end user would).

```bash
# Local (Mac)
make build                           # build + start container
make verify                          # Phase 0 health checks
make test                            # run test suites

# Health verification
./ops/phase0_verify.sh               # auto-detect environment
./ops/phase0_verify.sh local         # force local mode

# Production (.211) — manual pull, no deploy scripts
ssh root@192.168.71.211
cd /opt/odin/runsodin/runsodin
docker compose pull && docker compose up -d
```

Production compose: `/opt/odin/runsodin/runsodin/docker-compose.yml`.

### Architecture

#### Backend (`backend/`)

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

#### Monitor Daemons

Each runs as a separate supervisord process:
- **mqtt_monitor.py** — Bambu Lab MQTT telemetry (subscribes to `device/{serial}/report`)
- **moonraker_monitor.py** — Klipper REST polling (3-sec interval)
- **prusalink_monitor.py** — Prusa HTTP polling
- **elegoo_monitor.py** — Elegoo SDCP protocol
- **vision_monitor.py** — Camera-based AI failure detection (priority 35, after go2rtc)

#### Frontend (`frontend/src/`)

- **App.jsx** — React Router layout + sidebar
- **api.js** — Fetch-based API client (`fetchAPI` wrapper) with all endpoint definitions
- **permissions.js** — Client-side RBAC permission checker
- **pages/** — ~20 page components (Dashboard, Printers, Jobs, Models, Orders, Spools, Timeline, Analytics, Settings, Detections, etc.)
- **hooks/useWebSocket.js** — Real-time state updates from backend
- **contexts/** — BrandingContext, LicenseContext

Stack: React 18, Vite 5, TailwindCSS 3, React Query 5, React Router 6, Recharts, Lucide icons. No TypeScript — plain JSX.

#### Docker (`docker/`)

- **supervisord.conf** — Manages all 7 processes with auto-restart
- **entrypoint.sh** — Auto-generates secrets, initializes DB (WAL mode), creates tables
- **go2rtc.yaml** — Camera streaming relay config

#### Data Flow

1. Printer monitors poll/subscribe and update SQLite on state changes
2. State changes trigger `printer_events.dispatch()` → alerts + WebSocket push
3. Frontend receives real-time updates via WebSocket, falls back to HTTP polling
4. Scheduler assigns pending jobs to printers by color-match score and availability

#### Vigil AI (`backend/vision_monitor.py`)

- DB tables: `vision_detections`, `vision_settings`, `vision_models` (created in `entrypoint.sh`, not SQLAlchemy)
- Frames stored at `/data/vision_frames/{printer_id}/`, served via `/api/vision/frames/`
- Models stored at `/data/vision_models/`, defaults copied from `backend/vision_models_default/` on first boot
- Default thresholds duplicated in vision_monitor.py, main.py API defaults, and SQL schema — keep in sync
- ONNX models tracked via **Git LFS** (`.gitattributes` tracks `*.onnx`) — GitHub rejects files >100 MB without LFS

### Code Conventions

- **Auth model**: Three tiers — no headers (blocked), API key only (perimeter), JWT+API key (full RBAC with viewer/operator/admin roles)
- **API prefix**: All routes under `/api/`. Versioned at `/api/v1/`. Swagger at `/api/v1/docs`, ReDoc at `/api/v1/redoc`
- **Database**: SQLite at `/data/odin.db`. Several tables created via raw SQL in `entrypoint.sh` (not in SQLAlchemy `models.py`): `users`, `groups`, `print_jobs`, `print_files`, `oidc_config`, `webhooks`, `vision_detections`, `vision_settings`, `vision_models`, `api_tokens`, `active_sessions`, `token_blacklist`, `quota_usage`, `model_revisions`, `report_schedules`, `timelapses`
- **Org settings**: Per-org config stored as JSON in `groups.settings_json`. Keys: `default_filament_type`, `default_filament_color`, `quiet_hours_*`, `webhook_url`, `webhook_type`, `branding_app_name`, `branding_logo_url`. Helper: `routers/orgs.py:_get_org_settings()` merges with `DEFAULT_ORG_SETTINGS`.
- **Secrets**: Auto-generated on first run, persisted in `/data/`. `ENCRYPTION_KEY` (Fernet), `JWT_SECRET_KEY`, `API_KEY`
- **Pre-commit**: gitleaks for secret scanning
- **Git LFS**: Required for binary assets >100 MB (ONNX models). `*.onnx` tracked in `.gitattributes`
- **Version sources**: `VERSION` file is source of truth; also in `frontend/package.json`, `backend/main.py __version__`, `docker-compose.yml` image tag
- **License**: BSL 1.1 (converts to Apache 2.0 on 2029-02-07). Cannot offer as hosted service to third parties.
- **Adding API endpoints**: Backend route in `routers/` → frontend function in `api.js` using `fetchAPI` wrapper (not Axios). Always add both sides.
- **Code style**: No strict linter. Python follows existing patterns in routers. React uses functional components with hooks + TailwindCSS (no CSS modules).

### Server Topology

- **Local (Mac)**: Primary dev/test/release environment (Docker Desktop)
- **Sandbox**: `.200` — optional hardware staging instance
- **Production**: `.211` — live instance (pulls from GHCR)
- **License Manager**: `.6` — issues and verifies Ed25519 licenses (`/opt/odin-license-manager/`)

When debugging cross-server issues, always confirm which server a request originates from and which it targets. Trace the full request path before suggesting fixes.

### Release Workflow

Release pipeline: `make build` → `make test` → `make release VERSION=X.Y.Z` (bump → push → GHCR build). Prod (.211) pulls released images manually as an end user would (`docker compose pull && docker compose up -d`).

When deploying or completing a feature, ALWAYS bump the version and create a git tag. Never suggest skipping version bumps — if code is going live, it gets a new version.

### Seed Data & Demo Scripts

When creating seed data or demo data, validate against actual model schemas and enum definitions in `models.py` and `schemas.py` BEFORE writing the script. Check enum case sensitivity, field types (dict vs list), and ensure no references to non-existent resources (e.g., fake printer IPs that monitors will try to reach). Prefer minimal, realistic seed data over comprehensive fake data.

### Key Files

| File | Purpose |
|------|---------|
| `CLAUDE.md` | This file — project context and rules for Claude |
| `README.md` | Public-facing project description with install guide |
| `ROADMAP.md` | Feature roadmap with shipped/backlog/parked sections |
| `CHANGELOG.md` | Version history with dates and changes |
| `FEATURES.md` | Full feature catalog (680 lines, 22 sections) |
| `GTM.md` | Go-to-market strategy |
| `GTM_PLAN.md` | Detailed GTM execution plan (418 lines) |
| `VERSION` | Source of truth for version number |
| `../MASTER_PORTFOLIO.md` | Portfolio-wide status and priorities |

---

## Section 3: Engineering Standards

### Commit Discipline

**When to commit:** After completing a logical unit of work — a feature, a bug fix, a refactor, a doc update. NOT after every file save or minor tweak.

**Commit message format:**
```
type(scope): short description

- Detail 1
- Detail 2
```

**Types:** `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `style`

**Examples:**
- `feat(scheduler): add blackout hours support`
- `fix(auth): refresh token expiry off by one hour`
- `docs(roadmap): move job scheduling to shipped`
- `test(api): add RBAC endpoint coverage`

**Never commit:**
- Untested feature code (write at least a basic test first)
- With messages like "bump", "update", "fix stuff", "wip", "changes"
- Secrets, API keys, or credentials
- Co-author lines

**Before suggesting a commit, Claude must verify:**
1. All relevant tests pass
2. The commit message follows the format above
3. Doc updates are included if applicable (see below)

### Push Policy

**Push after every commit** unless actively working on a multi-commit branch. Claude should push automatically after committing — don't wait for Hugh to remember. Before pushing, verify no sensitive files are staged (CLAUDE.md, GTM.md, PROJECTIONS.md, .env).

### Doc Update Rules

**On every commit that includes `feat` or `fix`:**
- Update `ROADMAP.md` — move items from "Next Up" to "Shipped" if completed
- Update `CHANGELOG.md` — add entry under current version

**On version bump:**
- All of the above, plus:
- Update `README.md` version badge/reference if present
- Update `../MASTER_PORTFOLIO.md` — version number in the Status Dashboard table and the product's section header

**On status change** (e.g., pre-launch → live, new blocker identified, pricing change):
- Update `../MASTER_PORTFOLIO.md` — Status Dashboard table, product section, and Execution Priority if affected

**On architectural or strategic decisions:**
- Add a brief note to `CHANGELOG.md` or a `DECISIONS.md` file
- If it affects the portfolio (pricing, GTM, target customer, competitive positioning), update `../MASTER_PORTFOLIO.md`

**Claude should never say "don't forget to update the docs."** Claude should just do it as part of the commit preparation. The human should not have to remember.

### Testing Standards

- Every new feature should have at least one test covering the happy path
- Bug fixes should include a regression test
- Don't skip tests to ship faster — it costs more time later
- Report test count in commit messages for features: `feat(camera): add PiP mode (tests: 993 → 1001)`

### Git & Privacy Rules

**For public repos (this repo is public):**
```gitignore
# Business-sensitive docs — do not push to public repos
CLAUDE.md
GTM.md
PROJECTIONS.md
```

`README.md`, `CHANGELOG.md`, and `ROADMAP.md` are safe for public repos.

The master portfolio (`../MASTER_PORTFOLIO.md`) and CLAUDE template (`../CLAUDE_TEMPLATE.md`) must NEVER be committed to any remote repo.

**Before any `git push` to a public repo, Claude must verify** that no sensitive files are staged.

### Code Quality

- No TODO comments without a linked issue or roadmap item
- No dead code — delete it, don't comment it out. Git remembers.
- Functions over 50 lines should probably be broken up
- If you're copy-pasting code, it should be a function

---

## Section 4: Standard Doc Formats

If any of these files don't exist in the repo, Claude should create them on the first commit that warrants it.

### ROADMAP.md Format

```markdown
# [Product Name] — Roadmap

**Last updated:** [Date]

## Shipped
- [x] Feature name — brief description (v1.2.0, 2026-02-15)

## Next Up
- [ ] Feature name — brief description [PRIORITY: high/medium/low]

## Backlog
- [ ] Feature name — brief description

## Parked
- [ ] Feature name — reason it's parked
```

### CHANGELOG.md Format

```markdown
# Changelog

## [v1.2.0] — 2026-02-15
### Added
- Feature description

### Fixed
- Bug description

### Changed
- Change description

## [v1.1.0] — 2026-02-01
...
```

### DECISIONS.md Format (Optional)

```markdown
# Architecture & Strategy Decisions

## 2026-02-15 — Switched from SQLite to PostgreSQL
**Context:** Needed multi-tenant RLS for FFL isolation.
**Decision:** PostgreSQL 16 with async SQLAlchemy.
**Tradeoffs:** More ops overhead, but RLS is non-negotiable for compliance.
```
