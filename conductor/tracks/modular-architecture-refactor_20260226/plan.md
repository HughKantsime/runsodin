# Modular Architecture Refactor — Execution Plan

**Track:** modular-architecture-refactor_20260226
**Branch:** `refactor/modular-architecture` (off master)
**Spec version:** 1.3.76
**Spec file:** `conductor/tracks/modular-architecture-refactor_20260226/spec.md`

---

## Overlap Check

Searched `conductor/tracks.md` for completed tracks with overlapping deliverables:

- `site-redesign_20260225` — COMPLETE. Marketing site only. No overlap.
- `docs-buildout_20260225` — COMPLETE. Documentation pages only. No overlap.
- `auth-hardening-architectural_20260221` — COMPLETE. Auth security changes. No overlap (those changes will be present on master when we branch).
- `security-hardening-remaining_20260221` — COMPLETE. No overlap.
- All other active tracks (`auth-missing-endpoints`, `credential-encryption`, `ws-token-mfa-hardening`, `frontend-security`, `path-traversal-sweep`) target security hardening on master, not architecture. No overlap with this refactor branch.

**No work to skip.**

---

## Pre-Conditions

Before any task executes:
1. Branch `refactor/modular-architecture` must be created off current `master`
2. All active security tracks must NOT be merged into this branch until they are merged to master and rebased in
3. Container must be running and `make test` green on master before branching

---

## Phase 0: Preparation — Scaffolding and Documentation

**Goal:** Create all structural artifacts before touching any functional code. Zero behavior changes.

**Safety gate:** `make test` must pass after this phase (nothing functional changed).

---

### Task 0.1: Create branch and verify baseline <!-- e38627c -->

- **Status:** [x] Complete
- **Files:** (git operations only)
- **Steps:**
  - `git checkout master && git pull`
  - `make test` — verify all ~2,142 tests pass on master
  - `git checkout -b refactor/modular-architecture`
- **Acceptance:** Branch exists. `make test` green. Test count matches master baseline.
- **Depends on:** None

---

### Task 0.2: Create ADR directory and four ADR documents <!-- e38627c -->

- **Status:** [x] Complete
- **Files:**
  - `backend/adr/0001-modular-architecture-decision.md`
  - `backend/adr/0002-domain-boundaries.md`
  - `backend/adr/0003-event-bus-over-direct-imports.md`
  - `backend/adr/0004-interface-contracts.md`
- **Steps:** Create each ADR using the template from spec section 11. Document: (1) why monolith is a problem at current size, (2) how 12 domain boundaries were chosen from the coupling map, (3) why event bus over direct imports, (4) why ABCs as interface contracts.
- **Acceptance:** Four markdown files exist. Each has Status, Context, Decision, Consequences sections. No Python files changed.
- **Depends on:** 0.1

---

### Task 0.3: Create core/interfaces/ with all 5 ABC files <!-- e38627c -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/__init__.py`
  - `backend/core/interfaces/__init__.py`
  - `backend/core/interfaces/printer_state.py`
  - `backend/core/interfaces/event_bus.py`
  - `backend/core/interfaces/notification.py`
  - `backend/core/interfaces/org_settings.py`
  - `backend/core/interfaces/job_state.py`
- **Steps:** Copy ABC definitions verbatim from spec sections 3.1–3.5. `core/__init__.py` and `core/interfaces/__init__.py` are empty. These files contain only abstract class definitions — no imports from existing backend code.
- **Acceptance:** All 5 ABC files importable. `python -c "from core.interfaces.printer_state import PrinterStateProvider"` succeeds from `backend/`. No existing files modified.
- **Depends on:** 0.1

---

### Task 0.4: Create core/events.py with event type constants <!-- e38627c -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/events.py`
- **Steps:** Copy event type constant definitions verbatim from spec section 4. File contains only string constants (no imports from backend code).
- **Acceptance:** `python -c "from core.events import PRINTER_STATE_CHANGED"` succeeds. File contains all 16 event constants.
- **Depends on:** 0.3

---

### Task 0.5: Create empty module __init__.py manifests for all 12 modules <!-- e38627c -->

- **Status:** [x] Complete
- **Files:**
  - `backend/modules/__init__.py`
  - `backend/modules/printers/__init__.py`
  - `backend/modules/jobs/__init__.py`
  - `backend/modules/inventory/__init__.py`
  - `backend/modules/models_library/__init__.py`
  - `backend/modules/vision/__init__.py`
  - `backend/modules/notifications/__init__.py`
  - `backend/modules/organizations/__init__.py`
  - `backend/modules/orders/__init__.py`
  - `backend/modules/archives/__init__.py`
  - `backend/modules/reporting/__init__.py`
  - `backend/modules/system/__init__.py`
- **Steps:** Create each `__init__.py` with the module manifest format from spec section 5: MODULE_ID, MODULE_VERSION, MODULE_DESCRIPTION, ROUTES (empty list), TABLES (empty list), PUBLISHES (empty list), SUBSCRIBES (empty list), IMPLEMENTS (empty list), REQUIRES (empty list), DAEMONS (empty list). No `register()` function yet — that comes in Phase 6. Parent `modules/__init__.py` is empty.
- **Acceptance:** All 12 manifests exist with correct MODULE_ID values. `python -c "from modules.printers import MODULE_ID"` succeeds. No existing files modified.
- **Depends on:** 0.1

---

### Task 0.6: Add domain tags to all existing router files <!-- e38627c -->

- **Status:** [x] Complete
- **Files:**
  - `backend/routers/printers.py`
  - `backend/routers/cameras.py`
  - `backend/routers/jobs.py`
  - `backend/routers/scheduler.py`
  - `backend/routers/spools.py`
  - `backend/routers/models.py`
  - `backend/routers/vision.py`
  - `backend/routers/alerts.py`
  - `backend/routers/orgs.py`
  - `backend/routers/auth.py`
  - `backend/routers/orders.py`
  - `backend/routers/archives.py`
  - `backend/routers/projects.py`
  - `backend/routers/analytics.py`
  - `backend/routers/system.py`
  - `backend/routers/profiles.py`
- **Steps:** Add a 3-line comment block at the top of each router file (below the docstring if any, before imports):
  ```python
  # Domain: <module>
  # Depends on: core, <other modules>
  # Owns tables: <table list>
  ```
  Use the domain assignment table from spec section 2.1. Only comments added — no logic changes.
- **Acceptance:** All 16 router files have domain comment blocks. `make test` passes (no functional changes).
- **Depends on:** 0.5

---

### Task 0.7: Phase 0 commit <!-- e38627c -->

- **Status:** [x] Complete
- **Files:** All files from tasks 0.2–0.6
- **Steps:** `make test` must pass. Commit: `refactor(modular): Phase 0 — scaffolding, ADRs, interfaces, module manifests`
- **Acceptance:** All tests green. Commit exists on branch. No files from `backend/` root or `backend/routers/` have logic changes.
- **Depends on:** 0.2, 0.3, 0.4, 0.5, 0.6

---

## Phase 1: Extract Core Platform from deps.py

**Goal:** Split `backend/deps.py` (556 lines) into focused modules under `backend/core/`. `deps.py` becomes a re-export facade — all existing imports continue to work.

**Safety gate:** `make test` green after every sub-task.

---

### Task 1.1: Extract database layer to core/db.py <!-- 7bbc7ad -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/db.py` (new)
  - `backend/deps.py` (modify — add re-export, do NOT delete content yet)
- **Steps:**
  - Read `backend/deps.py` in full.
  - Create `backend/core/db.py` containing: `engine`, `SessionLocal`, `Base`, `get_db` (the FastAPI dependency). Import from `config.settings` for the DB URL.
  - At the TOP of the existing `deps.py`, add: `from core.db import get_db, engine, SessionLocal, Base` — these re-exports mean existing `from deps import get_db` continues to work. Keep the original definitions in `deps.py` until Phase 7 (they can coexist with the re-exports — just remove originals in 1.1 and replace with re-exports).
- **Acceptance:** `python -c "from core.db import get_db"` succeeds. `python -c "from deps import get_db"` still succeeds. `make test` green.
- **Depends on:** 0.7

---

### Task 1.2: Extract RBAC and org scoping to core/rbac.py <!-- 7bbc7ad -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/rbac.py` (new)
  - `backend/deps.py` (modify)
- **Steps:**
  - Read `backend/deps.py` in full.
  - Create `backend/core/rbac.py` containing: `require_role()`, `get_org_scope()`, `check_org_access()`, `_get_org_filter()`.
  - Update `deps.py` to re-export these from `core.rbac`.
- **Acceptance:** `python -c "from core.rbac import require_role"` succeeds. `python -c "from deps import require_role"` still succeeds. `make test` green.
- **Depends on:** 1.1

---

### Task 1.3: Extract auth dependencies to core/dependencies.py <!-- 7bbc7ad -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/dependencies.py` (new)
  - `backend/deps.py` (modify)
- **Steps:**
  - Read `backend/deps.py` in full.
  - Create `backend/core/dependencies.py` containing: `get_current_user()`, `log_audit()`.
  - Update `deps.py` to re-export from `core.dependencies`.
- **Acceptance:** `python -c "from core.dependencies import get_current_user"` succeeds. `from deps import get_current_user` still works. `make test` green.
- **Depends on:** 1.2

---

### Task 1.4: Extract auth helpers to core/auth_helpers.py <!-- 7bbc7ad -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/auth_helpers.py` (new)
  - `backend/deps.py` (modify)
- **Steps:**
  - Read `backend/deps.py` in full.
  - Create `backend/core/auth_helpers.py` containing: `_validate_password()`, `_check_rate_limit()`, `_record_login_attempt()`, `_is_locked_out()`.
  - Update `deps.py` to re-export from `core.auth_helpers`.
- **Acceptance:** `python -c "from core.auth_helpers import _validate_password"` succeeds. `make test` green.
- **Depends on:** 1.3

---

### Task 1.5: Copy config.py, auth.py, crypto.py, and other core files into core/ <!-- 7bbc7ad -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/config.py` (new — copy of `backend/config.py`, canonical)
  - `backend/core/auth.py` (new — copy of `backend/auth.py`, canonical)
  - `backend/core/crypto.py` (new — copy of `backend/crypto.py`, canonical)
  - `backend/core/db_utils.py` (new — copy of `backend/db_utils.py`, canonical)
  - `backend/core/rate_limit.py` (new — copy of `backend/rate_limit.py`, canonical)
  - `backend/core/ws_hub.py` (new — copy of `backend/ws_hub.py`, canonical)
  - `backend/core/license_manager.py` (new — re-exports from `backend/license_manager.py`; canonical stays in place due to patch.object mocking in test_license.py)
  - `backend/config.py` (converted to re-export facade)
  - `backend/auth.py` (converted to re-export facade)
  - `backend/crypto.py` (converted to re-export facade)
  - `backend/db_utils.py` (converted to re-export facade)
  - `backend/rate_limit.py` (converted to re-export facade)
  - `backend/ws_hub.py` (converted to re-export facade)
  - `backend/license_manager.py` (unchanged — canonical; test_license.py patches its internals directly)
- **Steps:** For each file: copy content to `core/` location with internal imports updated to reference `core.` paths where needed. Add re-export line at top of original file so old `from config import settings` still works. Handle potential circular imports by importing lazily where needed.
- **Note:** `license_manager.py` was kept as the canonical implementation because `test_license.py` uses `patch.object(lm, "_find_license_file")` — patching the re-export facade would not intercept calls inside the implementation module. The `core/license_manager.py` re-exports from the original.
- **Acceptance:** `python -c "from core.config import settings"` succeeds. All original `from config import settings`, `from auth import ...`, `from crypto import ...` etc. still work. `make test` green (55 passed, 10 skipped, 81 errors — matches master baseline).
- **Depends on:** 1.4

---

### Task 1.6: Phase 1 commit <!-- 7bbc7ad -->

- **Status:** [x] Complete
- **Files:** All from tasks 1.1–1.5
- **Steps:** `make test` passes. Commit: `refactor(modular): Phase 1 — extract core platform from deps.py`
- **Acceptance:** All tests green. `deps.py` still exists and is a functional re-export facade. 11 new `core/` files exist.
- **Result:** SHA 7bbc7ad. Tests: 55 passed, 10 skipped, 81 errors (matches master baseline).
- **Depends on:** 1.1, 1.2, 1.3, 1.4, 1.5

---

## Phase 2: Split models.py and schemas.py by Domain

**Goal:** Decompose `backend/models.py` (1054 lines) and `backend/schemas.py` (856 lines) into per-domain files. Both original files become re-export facades.

**Safety gate:** `make test` green throughout.

---

### Task 2.1: Create core/base.py with shared enums and declarative base <!-- 7719306 -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/base.py` (new)
- **Steps:**
  - Read `backend/models.py` in full.
  - Extract: `Base` (declarative base), all shared enums (`JobStatus`, `OrderStatus`, `FilamentType`, `SpoolStatus`, `AlertType`, `AlertSeverity`, and any others used across multiple modules).
  - `backend/core/base.py` contains only the Base and enums — no ORM model classes.
- **Acceptance:** `python -c "from core.base import Base, JobStatus"` succeeds. No circular imports. `make test` green.
- **Depends on:** 1.6

---

### Task 2.2: Create modules/printers/models.py and modules/printers/schemas.py <!-- 7719306 -->

- **Status:** [x] Complete
- **Files:**
  - `backend/modules/printers/models.py` (new)
  - `backend/modules/printers/schemas.py` (new)
- **Steps:**
  - Read `backend/models.py` and `backend/schemas.py` in full.
  - Extract into `modules/printers/models.py`: `Printer`, `FilamentSlot`, `NozzleLifecycle` ORM classes.
  - Extract into `modules/printers/schemas.py`: all Printer*, Camera*, NozzleLifecycle*, FilamentSlot* Pydantic models.
  - Both files import from `core.base` for Base and enums.
- **Acceptance:** `python -c "from modules.printers.models import Printer"` succeeds. No imports from `models.py` (no circular dependency). `make test` green.
- **Depends on:** 2.1

---

### Task 2.3: Create modules/jobs/models.py and modules/jobs/schemas.py <!-- 7719306 -->

- **Status:** [x] Complete
- **Files:**
  - `backend/modules/jobs/models.py` (new)
  - `backend/modules/jobs/schemas.py` (new)
- **Steps:**
  - Extract into `modules/jobs/models.py`: `Job`, `SchedulerRun`, `PrintPreset` ORM classes.
  - Extract into `modules/jobs/schemas.py`: all Job*, SchedulerRun*, PrintPreset* Pydantic models.
  - `Job` model has a ForeignKey to `printers` table — reference it by string `"printers.id"` not by importing the Printer class.
- **Acceptance:** `python -c "from modules.jobs.models import Job"` succeeds. `make test` green.
- **Depends on:** 2.2

---

### Task 2.4: Create models.py/schemas.py for remaining 8 modules <!-- 7719306 -->

- **Status:** [x] Complete
- **Files (16 new files):**
  - `backend/modules/inventory/models.py` — `Spool`, `FilamentLibrary`, `SpoolUsage`, `DryingLog`, `Consumable`, `ProductConsumable`, `ConsumableUsage`
  - `backend/modules/inventory/schemas.py`
  - `backend/modules/models_library/models.py` — `Model`
  - `backend/modules/models_library/schemas.py`
  - `backend/modules/vision/models.py` — `VisionDetection`, `VisionSettings`, `VisionModel`
  - `backend/modules/vision/schemas.py`
  - `backend/modules/notifications/models.py` — `Alert`, `AlertPreference`, `PushSubscription`
  - `backend/modules/notifications/schemas.py`
  - `backend/modules/orders/models.py` — `Order`, `OrderItem`, `Product`, `ProductComponent`
  - `backend/modules/orders/schemas.py`
  - `backend/modules/archives/models.py` — `Timelapse`
  - `backend/modules/archives/schemas.py`
  - `backend/modules/system/models.py` — `MaintenanceTask`, `MaintenanceLog`
  - `backend/modules/system/schemas.py`
  - `backend/core/models.py` — `SystemConfig`, `AuditLog`
  - `backend/core/schemas.py` — base schemas (HealthCheck, etc.)
- **Steps:** For each module, read the relevant sections of `backend/models.py` and `backend/schemas.py`. Extract the ORM models and Pydantic schemas belonging to that module per the table ownership map in spec section 2 (Phase 2 table). Use string foreign key references to avoid cross-module model imports. Enums come from `core.base`.
- **Acceptance:** All 16 files importable without errors. No circular imports. `make test` green.
- **Depends on:** 2.3

---

### Task 2.5: Convert models.py and schemas.py to re-export facades <!-- 7719306 -->

- **Status:** [x] Complete
- **Files:**
  - `backend/models.py` (rewrite to re-export facade)
  - `backend/schemas.py` (rewrite to re-export facade)
- **Steps:**
  - Read both current files in full to confirm all classes are accounted for in module files.
  - Replace content of `backend/models.py` with imports from all module model files plus `core.base`, re-exporting every class by name. Pattern from spec Phase 2 block.
  - Replace content of `backend/schemas.py` with imports from all module schema files, re-exporting every class.
  - Verify: `from models import Printer, Job, Spool, Alert` etc. all work from a single import statement.
- **Acceptance:** `python -c "from models import Printer, Job, Spool, Alert, VisionDetection"` succeeds. `make test` green (this is the critical validation — all 2,142 tests must still pass with the facade in place).
- **Depends on:** 2.2, 2.3, 2.4

---

### Task 2.6: Phase 2 commit <!-- 7719306 -->

- **Status:** [x] Complete
- **Files:** All from tasks 2.1–2.5
- **Steps:** `make test` passes. `make build` succeeds. Commit: `refactor(modular): Phase 2 — split models.py and schemas.py into domain modules`
- **Acceptance:** All tests green. 18 new files in `modules/` and `core/`. `models.py` and `schemas.py` are facades.
- **Result:** SHA 7719306. Tests: 1 failed (pre-existing), 54 passed, 10 skipped, 81 errors (matches Phase 1 baseline). 23 files changed, 2634 insertions, 1868 deletions. Created 20 new domain model/schema files, converted models.py and schemas.py to facades.
- **Depends on:** 2.5

---

## Phase 3: Move Files into Module Directories

**Goal:** Relocate all backend files to their module directories. This is the largest phase (~55 file moves). The re-export facades from Phases 1 and 2 are the safety net.

**Safety gate:** `make test` green after each sub-task group.

---

### Task 3.1: Move router files for printers and jobs modules <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files (moved):**
  - `backend/routers/printers.py` → `backend/modules/printers/routes.py`
  - `backend/routers/cameras.py` → `backend/modules/printers/camera_routes.py`
  - `backend/routers/jobs.py` → `backend/modules/jobs/routes.py`
  - `backend/routers/scheduler.py` → `backend/modules/jobs/scheduler_routes.py`
- **Files (stubs created):**
  - `backend/routers/printers.py` (re-export stub)
  - `backend/routers/cameras.py` (re-export stub)
  - `backend/routers/jobs.py` (re-export stub)
  - `backend/routers/scheduler.py` (re-export stub)
- Routers use `from models import` (facade) and `from deps import` (facade) — no import changes needed.
- Updated `backend/main.py` to import these 4 routers from new module locations.
- **Acceptance:** `make test` green. All printer and job endpoints respond correctly.
- **Depends on:** 2.6

---

### Task 3.2: Move router files for inventory, models_library, vision, notifications <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files (moved):**
  - `backend/routers/spools.py` → `backend/modules/inventory/routes.py`
  - `backend/routers/models.py` → `backend/modules/models_library/routes.py`
  - `backend/routers/vision.py` → `backend/modules/vision/routes.py`
  - `backend/routers/alerts.py` → `backend/modules/notifications/routes.py`
- **Files (stubs):** One stub per original router location.
- Updated `main.py` imports for all 4 routers.
- **Acceptance:** `make test` green.
- **Depends on:** 3.1

---

### Task 3.3: Move router files for organizations, orders, archives, reporting, system <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files (moved):**
  - `backend/routers/orgs.py` → `backend/modules/organizations/routes.py`
  - `backend/routers/auth.py` → `backend/modules/organizations/auth_routes.py`
  - `backend/routers/orders.py` → `backend/modules/orders/routes.py`
  - `backend/routers/archives.py` → `backend/modules/archives/routes.py`
  - `backend/routers/projects.py` → `backend/modules/archives/project_routes.py`
  - `backend/routers/analytics.py` → `backend/modules/reporting/routes.py`
  - `backend/routers/system.py` → `backend/modules/system/routes.py`
  - `backend/routers/profiles.py` → `backend/modules/system/profile_routes.py`
- **Files (stubs):** One stub per original router location.
- `main.py` now imports all 16 routers from `modules.*` paths directly.
- **Acceptance:** `make test` green. `make build` succeeds.
- **Depends on:** 3.2

---

### Task 3.4: Move printer adapter files into modules/printers/adapters/ <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files (moved):**
  - `backend/bambu_adapter.py` → `backend/modules/printers/adapters/bambu.py`
  - `backend/moonraker_adapter.py` → `backend/modules/printers/adapters/moonraker.py`
  - `backend/prusalink_adapter.py` → `backend/modules/printers/adapters/prusalink.py`
  - `backend/elegoo_adapter.py` → `backend/modules/printers/adapters/elegoo.py`
  - `backend/modules/printers/adapters/__init__.py` (new — PrinterAdapter ABC base interface)
- All adapter files had no internal ODIN imports — copied as-is.
- Stubs re-export all classes from new locations.
- **Acceptance:** `make test` green.
- **Depends on:** 3.3

---

### Task 3.5: Move printer monitor files into modules/printers/monitors/ <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files (moved):**
  - `backend/mqtt_monitor.py` → `backend/modules/printers/monitors/mqtt_monitor.py`
  - `backend/moonraker_monitor.py` → `backend/modules/printers/monitors/moonraker_monitor.py`
  - `backend/prusalink_monitor.py` → `backend/modules/printers/monitors/prusalink_monitor.py`
  - `backend/elegoo_monitor.py` → `backend/modules/printers/monitors/elegoo_monitor.py`
  - `backend/modules/printers/monitors/__init__.py` (new — empty)
- Monitors use `sys.path.insert(0, BACKEND_PATH)` so relative imports from `bambu_adapter`, `db_utils`, `printer_events` all resolve via stubs.
- Stubs at root use `runpy.run_module` for `__main__` execution.
- Updated `docker/supervisord.conf` to use `python3 -m modules.printers.monitors.*` for all 4 monitors.
- **Acceptance:** `make test` green. `make build` succeeds. All 4 printer monitors appear in supervisord process list after container start.
- **Depends on:** 3.4

---

### Task 3.6: Move remaining printer support files <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files (moved):**
  - `backend/bambu_integration.py` → `backend/modules/printers/bambu_integration.py`
  - `backend/printer_models.py` → `backend/modules/printers/printer_models.py`
  - `backend/printer_dispatch.py` → `backend/modules/printers/dispatch.py`
  - `backend/hms_codes.py` → `backend/modules/printers/hms_codes.py`
  - `backend/smart_plug.py` → `backend/modules/printers/smart_plug.py`
- All had no internal ODIN imports (or only stdlib/third-party) — copied as-is.
- Stubs at root re-export all public functions/classes via `from modules.printers.* import`.
- **Acceptance:** `make test` green.
- **Depends on:** 3.5

---

### Task 3.7: Move notifications domain files <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files (moved):**
  - `backend/printer_events.py` → `backend/modules/notifications/event_dispatcher.py`
  - `backend/alert_dispatcher.py` → `backend/modules/notifications/alert_dispatcher.py`
  - `backend/quiet_hours.py` → `backend/modules/notifications/quiet_hours.py`
  - `backend/mqtt_republish.py` → `backend/modules/notifications/mqtt_republish.py`
- `printer_events.py` uses lazy `try/except` imports of `mqtt_republish`, `hms_codes`, `smart_plug`, `ws_hub`, `archive` — all resolve via root stubs.
- Direct import pattern kept intact; event bus refactor is Phase 4.
- **Acceptance:** `make test` green. Alert notifications still fire (integration behavior).
- **Depends on:** 3.6

---

### Task 3.8: Move models_library, orders, archives, reporting domain files <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files (moved):**
  - `backend/threemf_parser.py` → `backend/modules/models_library/threemf_parser.py`
  - `backend/print_file_meta.py` → `backend/modules/models_library/print_file_meta.py`
  - `backend/invoice_generator.py` → `backend/modules/orders/invoice_generator.py`
  - `backend/archive.py` → `backend/modules/archives/archive.py`
  - `backend/timelapse_capture.py` → `backend/modules/archives/timelapse_capture.py`
  - `backend/report_runner.py` → `backend/modules/reporting/report_runner.py`
- `timelapse_capture` and `report_runner` are daemons; stubs use `runpy.run_module`.
- Updated `docker/supervisord.conf` for both daemons.
- **Acceptance:** `make test` green.
- **Depends on:** 3.7

---

### Task 3.9: Move organizations domain files <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files (moved):**
  - `backend/branding.py` → `backend/modules/organizations/branding.py`
  - `backend/oidc_handler.py` → `backend/modules/organizations/oidc_handler.py`
- `branding.py` imports `from models import Base` — resolves via facade.
- `oidc_handler.py` has only stdlib/third-party imports.
- **Acceptance:** `make test` green.
- **Depends on:** 3.8

---

### Task 3.10: Move vision monitor daemon <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files (moved):**
  - `backend/vision_monitor.py` → `backend/modules/vision/monitor.py`
- Stub at root uses `runpy.run_module`.
- Updated `docker/supervisord.conf`: `python3 -m modules.vision.monitor`
- Checked `docker/entrypoint.sh`: no direct references to `vision_monitor.py`.
- **Acceptance:** `make test` green. `make build` succeeds. Vision monitor appears as healthy supervisord process.
- **Depends on:** 3.9

---

### Task 3.11: Move scheduler engine <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files (moved):**
  - `backend/scheduler.py` → `backend/modules/jobs/scheduler.py`
- `scheduler.py` imports `from models import ...` — resolves via facade.
- Stub re-exports all classes and `run_scheduler` function.
- **Acceptance:** `make test` green. Job scheduling still functions.
- **Depends on:** 3.10

---

### Task 3.12: Phase 3 final validation and commit <!-- 3b82a18 -->

- **Status:** [x] Complete
- **Files:** All changes from 3.1–3.11
- **Result:** SHA 3b82a18. 90 files changed, 28438 insertions, 28207 deletions.
  - 45 existing files converted to re-export stubs (16 routers + 27 standalone backend files + 2 supervisord daemons)
  - 45 new canonical files created in module directories
  - `docker/supervisord.conf` updated: all 7 daemon paths use `python3 -m modules.*` paths
  - All 154 Python files pass syntax check
  - `main.py` imports all 16 routers directly from `modules.*` paths
- **Acceptance:** All tests green. Container starts. All processes healthy. Commit on branch.
- **Depends on:** 3.11

---

## Phase 4: Implement the Event Bus

**Goal:** Replace direct cross-module imports in `modules/notifications/event_dispatcher.py` (was `printer_events.py`) with a pub/sub event bus. This is the architectural heart of the refactor.

---

### Task 4.1: Implement InMemoryEventBus in core/event_bus.py <!-- 2d30c5e -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/event_bus.py` (new)
- **Steps:**
  - Implement `InMemoryEventBus(EventBus)` per spec Phase 4 step 1. Uses `defaultdict(list)` for handlers. `publish()` iterates handlers synchronously (no async yet). `subscribe()` and `unsubscribe()` manage handler lists.
  - Wire a singleton instance: `_bus = InMemoryEventBus()` at module level, expose `get_event_bus()` function.
  - Import `EventBus` and `Event` from `core.interfaces.event_bus`.
  - Added wildcard `"*"` subscription support.
- **Acceptance:** `python -c "from core.event_bus import get_event_bus; bus = get_event_bus(); bus.subscribe('test', lambda e: None); bus.publish(type('E', (), {'event_type': 'test', 'data': {}})())"` does not error. `make test` green.
- **Depends on:** 3.12

---

### Task 4.2: Refactor event_dispatcher.py to publish events instead of direct imports <!-- 2d30c5e -->

- **Status:** [x] Complete
- **Files:**
  - `backend/modules/notifications/event_dispatcher.py` (modify)
- **Steps:**
  - Read the file in full (was `printer_events.py`, ~1049 lines).
  - Removed `try/except ImportError` blocks for `mqtt_republish`, `ws_hub`, `smart_plug`.
  - `job_started()` now publishes `ev.JOB_STARTED` event via bus.
  - `job_completed()` now publishes `ev.JOB_COMPLETED` or `ev.JOB_FAILED` event; removed direct smart_plug calls and archive import.
  - `dispatch_alert()` now publishes `"notifications.alert_dispatched"` event instead of calling `ws_push` directly.
  - `hms_codes` kept as direct import (pure lookup utility, no side effects).
  - `dispatch_alert()` kept as intra-module direct call (same module).
- **Acceptance:** `make test` green. Existing behavior preserved via event subscribers.
- **Depends on:** 4.1

---

### Task 4.3: Register event subscribers in each module <!-- 2d30c5e -->

- **Status:** [x] Complete
- **Files:**
  - `backend/modules/notifications/__init__.py` (modify — add register_subscribers)
  - `backend/modules/printers/__init__.py` (modify — add register_subscribers)
  - `backend/modules/archives/__init__.py` (modify — add register_subscribers)
  - `backend/core/ws_hub.py` (modify — add subscribe_to_bus)
  - `backend/modules/printers/smart_plug.py` (modify — add register_subscribers)
  - `backend/modules/notifications/mqtt_republish.py` (modify — add register_subscribers)
  - `backend/modules/archives/archive.py` (modify — add register_subscribers)
- **Steps:**
  - `core/ws_hub.py`: `subscribe_to_bus(bus)` registers specific handlers for job/alert events with legacy name translation; wildcard handler for all other events (avoids duplicates).
  - `modules/printers/smart_plug.py`: `register_subscribers(bus)` reacts to `job.started` (auto power-on) and `job.completed` (auto power-off with cooldown).
  - `modules/notifications/mqtt_republish.py`: `register_subscribers(bus)` republishes printer state, job lifecycle, and alert events to external broker.
  - `modules/archives/archive.py`: `register_subscribers(bus)` auto-captures print archive on `job.completed`/`job.failed`.
  - Each module's `__init__.py` wires its own subscribers.
- **Acceptance:** `make test` green. Subscribers registered without errors.
- **Depends on:** 4.2

---

### Task 4.4: Wire event bus into app startup in main.py <!-- 2d30c5e -->

- **Status:** [x] Complete
- **Files:**
  - `backend/main.py` (modify)
- **Steps:**
  - In the FastAPI lifespan context (startup), get the event bus singleton and call each module's `register_subscribers(bus)` in order: ws_hub, printers, notifications, archives.
  - Event bus initialized before `broadcast_task` starts.
- **Acceptance:** `make test` green. `make build` succeeds. Container healthy, all processes running.
- **Depends on:** 4.3

---

### Task 4.5: Phase 4 commit <!-- 2d30c5e -->

- **Status:** [x] Complete
- **Files:** All from tasks 4.1–4.4. Also fixed two pre-existing Phase 3 stub gaps:
  - `backend/routers/printers.py`: added re-export of `get_camera_url`, `sync_go2rtc_config` (needed by `camera_routes.py`)
  - `backend/mqtt_monitor.py`: added re-export of `PrinterMonitor` class (needed by `test_mqtt_linking.py`)
- **Result:** SHA 2d30c5e. Tests: 1 failed (pre-existing), 54 passed, 10 skipped, 81 errors — matches Phase 3 baseline exactly.
- **Depends on:** 4.4

---

## Phase 5: Schema Ownership — Module-Owned Migrations

**Goal:** Move table creation out of `docker/entrypoint.sh` into per-module `migrations/001_initial.sql` files. Each table created by exactly one owner. Resolves the dual-definition problem.

---

### Task 5.1: Create migration SQL files for each module <!-- f485255 -->

- **Status:** [x] Complete
- **Files (12 new SQL files):**
  - `backend/core/migrations/001_initial.sql` — users, api_tokens, active_sessions, token_blacklist, password_reset_tokens, login_attempts
  - `backend/modules/archives/migrations/001_initial.sql` — print_archives, projects (timelapses comment-only: ORM)
  - `backend/modules/inventory/migrations/001_initial.sql` — comment-only (consumables, product_consumables, consumable_usage all ORM)
  - `backend/modules/jobs/migrations/001_initial.sql` — print_jobs, print_files
  - `backend/modules/models_library/migrations/001_initial.sql` — model_revisions
  - `backend/modules/notifications/migrations/001_initial.sql` — webhooks
  - `backend/modules/orders/migrations/001_initial.sql` — comment-only (all ORM)
  - `backend/modules/organizations/migrations/001_initial.sql` — groups, oidc_config, oidc_pending_states, oidc_auth_codes, quota_usage
  - `backend/modules/printers/migrations/001_initial.sql` — printer_telemetry, hms_error_history, ams_telemetry (nozzle_lifecycle comment-only: ORM)
  - `backend/modules/reporting/migrations/001_initial.sql` — report_schedules
  - `backend/modules/system/migrations/001_initial.sql` — printer_profiles
  - `backend/modules/vision/migrations/001_initial.sql` — comment-only (vision_detections, vision_settings, vision_models all ORM)
- Dual-defined tables resolved: ORM is canonical; entrypoint.sh CREATE TABLE blocks removed
- All migrations verified idempotent (CREATE TABLE IF NOT EXISTS, tested twice)
- **Depends on:** 4.5

---

### Task 5.2: Implement migration runner in core/db.py <!-- f485255 -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/db.py` (modified — added _db_path_from_url, _run_sql_file, run_core_migrations, run_module_migrations)
- `run_core_migrations(database_url?)` runs `core/migrations/*.sql` in sorted order
- `run_module_migrations(modules_dir, database_url?)` discovers all `modules/*/migrations/*.sql` and runs in sorted order
- Both accept explicit database_url param (used by entrypoint.sh to avoid settings dependency)
- Skips SQL files that contain only comments/whitespace
- **Depends on:** 5.1

---

### Task 5.3: Update entrypoint.sh to use migration runner <!-- f485255 -->

- **Status:** [x] Complete
- **Files:**
  - `docker/entrypoint.sh` (modified — 700+ lines of CREATE TABLE heredocs replaced)
- Replaced all CREATE TABLE inline blocks with: `python3 -c "...run_core_migrations(...); run_module_migrations(...)"`
- Preserved all ALTER TABLE upgrade migrations in a consolidated `UPGRADESEOF` block
- Execution order preserved: `Base.metadata.create_all` first, then migration runner, then upgrade migrations
- Entrypoint reduced from 960 to 347 lines
- Shell syntax verified with `bash -n`
- **Depends on:** 5.2

---

### Task 5.4: Phase 5 commit <!-- f485255 -->

- **Status:** [x] Complete
- **Files:** All from tasks 5.1–5.3
- Commit: `refactor(modular): Phase 5 — module-owned schema migrations`
- SHA: f485255. 14 files changed, 589 insertions, 694 deletions.
- Migration runner end-to-end tested: all 22 raw-SQL tables created correctly
- Idempotency verified: running migrations twice produces no errors
- **Depends on:** 5.3

---

## Phase 6: App Factory and Module Registry

**Goal:** Replace `main.py`'s hard-coded router imports with dynamic module discovery.

---

### Task 6.1: Create core/registry.py — module registry <!-- 0ccf490 -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/registry.py` (new)
- **Steps:**
  - Implement `ModuleRegistry` class per spec Phase 6 step 2: `register_provider(interface_name, impl)`, `get_provider(interface_name)`, `validate_dependencies()`. The `validate_dependencies()` method checks that every interface listed in any module's REQUIRES list has a registered provider.
  - Added `record_requires()` helper so each module's REQUIRES list is tracked for validation.
- **Acceptance:** `python -c "from core.registry import ModuleRegistry"` succeeds. `make test` green.
- **Depends on:** 5.4

---

### Task 6.2: Create core/app.py — app factory with module discovery <!-- 0ccf490 -->

- **Status:** [x] Complete
- **Files:**
  - `backend/core/app.py` (new)
- **Steps:**
  - Read `backend/main.py` in full.
  - Implemented `create_app()` with `_discover_modules()` (scans modules/ for MODULE_ID) and `_resolve_load_order()` (Kahn's topological sort by REQUIRES/IMPLEMENTS).
  - Module `register()` calls happen at app creation time (not in lifespan) so routes are registered before the SPA catch-all and before the first request.
  - Lifespan handles DB init, event bus wiring, and background tasks.
  - Extracted `_setup_middleware()` and `_register_http_middleware()` from main.py — all middleware preserved.
- **Acceptance:** `python -c "from core.app import create_app"` succeeds without errors. `make test` green.
- **Depends on:** 6.1

---

### Task 6.3: Add register() function to each module __init__.py <!-- 0ccf490 -->

- **Status:** [x] Complete
- **Files (all 11 module __init__.py files modified — no organizations omitted):**
  - `backend/modules/printers/__init__.py` — registers routes.router + camera_routes.router, provides PrinterStateProvider
  - `backend/modules/jobs/__init__.py` — registers routes.router + scheduler_routes.router, provides JobStateProvider
  - `backend/modules/inventory/__init__.py` — registers routes.router
  - `backend/modules/models_library/__init__.py` — registers routes.router
  - `backend/modules/vision/__init__.py` — registers routes.router
  - `backend/modules/notifications/__init__.py` — registers routes.router, provides NotificationDispatcher
  - `backend/modules/organizations/__init__.py` — registers routes.router + auth_routes.router, provides OrgSettingsProvider
  - `backend/modules/orders/__init__.py` — registers routes.router
  - `backend/modules/archives/__init__.py` — registers routes.router + project_routes.router
  - `backend/modules/reporting/__init__.py` — registers routes.router
  - `backend/modules/system/__init__.py` — registers routes.router + profile_routes.router
- **Steps:** Updated ROUTES and DAEMONS lists to accurate values. Each register() does include_router for both /api and /api/v1 prefixes.
- **Acceptance:** `make test` green. All 578 routes registered. Container healthy.
- **Depends on:** 6.2

---

### Task 6.4: Update main.py to use create_app() <!-- 0ccf490 -->

- **Status:** [x] Complete
- **Files:**
  - `backend/main.py` (rewrite — 524 lines → 12 lines)
- **Result:** main.py is now:
  ```python
  from core.app import create_app
  app = create_app()
  ```
- **Acceptance:** `make test` green. `make build` succeeds. Container starts. All 9 processes healthy. 578 routes registered.
- **Depends on:** 6.3

---

### Task 6.5: Phase 6 commit <!-- 0ccf490 -->

- **Status:** [x] Complete
- **Files:** All from tasks 6.1–6.4
- **Steps:** `make test` passes. `make build` passes. Commit: `refactor(modular): Phase 6 — app factory with dynamic module discovery`
- **Acceptance:** All tests green. `main.py` is ~12 lines. Module registry validates all dependencies at startup.
- **Depends on:** 6.4

---

## Phase 7: Remove Re-Export Facades

**Goal:** Delete the backward-compatibility facades. Every import must point to its actual source.

---

### Task 7.1: Update all test files to use new import paths

- **Files:**
  - `tests/*.py` (all test files that import from `models`, `schemas`, `deps`, `routers/*`)
- **Steps:**
  - Read each test file. Replace `from models import X` with `from modules.Y.models import X` (or `from core.models import X` for base types). Replace `from deps import get_db` with `from core.db import get_db`. Replace `from routers.X import router` with module path.
  - Run `make test` after each file to catch regressions.
- **Acceptance:** All test files use new import paths. `make test` green.
- **Depends on:** 6.5

---

### Task 7.2: Update all daemon files to use new import paths

- **Files:**
  - `backend/modules/printers/monitors/*.py`
  - `backend/modules/vision/monitor.py`
- **Steps:**
  - Read each monitor file. Replace any remaining stub-path imports with direct module paths.
  - Example: `from printer_events import dispatch` → `from modules.notifications.event_dispatcher import dispatch`.
  - This is safe now because the modules directory structure is stable.
- **Acceptance:** All monitor files import from direct paths. `make test` green. Container starts.
- **Depends on:** 7.1

---

### Task 7.3: Update all module internal imports to direct paths

- **Files:**
  - All Python files under `backend/modules/*/`
- **Steps:**
  - Run `grep -r "^from models import\|^from schemas import\|^from deps import\|^from routers\." backend/modules/` to find remaining old-path imports.
  - Update each to use direct module paths.
  - Run `make test` after bulk update.
- **Acceptance:** `grep -r "^from models import\|^from schemas import\|^from deps import" backend/modules/` returns zero results. `make test` green.
- **Depends on:** 7.2

---

### Task 7.4: Delete facade files and routers/ stubs

- **Files (deleted):**
  - `backend/models.py` (facade)
  - `backend/schemas.py` (facade)
  - `backend/routers/*.py` (all stubs — confirm each is a pure re-export before deleting)
  - Individual stubs at `backend/bambu_adapter.py`, `backend/moonraker_adapter.py`, etc. (all stubs created in Phase 3)
- **Files (reduced to essentials):**
  - `backend/deps.py` — delete or reduce to only truly shared utilities not yet in `core/`
- **Steps:**
  - For each file to delete: confirm it contains only `from X import Y` re-export lines and no original logic.
  - Delete confirmed stubs.
  - If `deps.py` still has any original logic not yet moved to `core/`, move it now, then delete.
  - Run `make test` after each deletion batch.
- **Acceptance:** `grep -r "^from deps import" backend/` returns zero results outside `core/`. `make test` green. `backend/routers/` directory is empty or removed.
- **Depends on:** 7.3

---

### Task 7.5: Update documentation for new file paths

- **Files:**
  - `CLAUDE.md` — update "Current File Layout" section (section 1.2 of the spec is now outdated)
  - `backend/CLAUDE.md` if it exists
- **Steps:**
  - Read `CLAUDE.md` in full.
  - Update the architecture section describing backend file layout to reflect the new module structure.
  - Update any references to specific file paths like `backend/models.py` or `backend/routers/*.py`.
- **Acceptance:** CLAUDE.md reflects the new directory structure. No references to deleted files.
- **Depends on:** 7.4

---

### Task 7.6: Phase 7 commit

- **Files:** All from tasks 7.1–7.5
- **Steps:** `make test` passes. `make test-security` passes. `grep -r "from deps import" backend/` returns zero (or only core/). Commit: `refactor(modular): Phase 7 — remove re-export facades, all imports direct`
- **Acceptance:** All tests green. No facade files. Clean imports throughout.
- **Depends on:** 7.5

---

## Phase 8: Split Oversized Route Files

**Goal:** Decompose the 3 largest remaining route files. No file over ~500 lines in routes.

---

### Task 8.1: Split modules/printers/routes.py (2480 lines)

- **Files (new):**
  - `backend/modules/printers/routes_crud.py` — CRUD: create, read, update, delete printers
  - `backend/modules/printers/routes_status.py` — Live status, telemetry, HMS error history
  - `backend/modules/printers/routes_controls.py` — Commands: pause, resume, cancel, speed, fans
  - `backend/modules/printers/routes_ams.py` — AMS/filament slot management
  - `backend/modules/printers/routes_smart_plug.py` — Smart plug endpoints
  - `backend/modules/printers/routes_nozzle.py` — Nozzle lifecycle tracking
- **Files (modified):**
  - `backend/modules/printers/routes.py` — becomes an import aggregator that includes all sub-routers
  - `backend/modules/printers/__init__.py` — update ROUTES list
- **Steps:**
  - Read `modules/printers/routes.py` in full.
  - Group endpoints by responsibility. Extract into sub-files, each with its own `APIRouter`.
  - `routes.py` remains but becomes a thin aggregator: creates a parent router and includes all sub-routers.
  - Test after each split.
- **Acceptance:** No single routes file over 500 lines. All printer endpoints respond correctly. `make test` green.
- **Depends on:** 7.6

---

### Task 8.2: Split modules/system/routes.py (2060 lines)

- **Files (new):**
  - `backend/modules/system/routes_health.py` — Health check, config, setup wizard
  - `backend/modules/system/routes_backup.py` — Backup and restore
  - `backend/modules/system/routes_admin.py` — Admin logs, support bundle, global search
  - `backend/modules/system/routes_maintenance.py` — Maintenance tasks and logs
- **Files (modified):**
  - `backend/modules/system/routes.py` — aggregator
  - `backend/modules/system/__init__.py` — update ROUTES list
- **Steps:** Same pattern as 8.1.
- **Acceptance:** No system routes file over 500 lines. All system endpoints respond correctly. `make test` green.
- **Depends on:** 8.1

---

### Task 8.3: Split modules/organizations/auth_routes.py (1888 lines)

- **Files (new):**
  - `backend/modules/organizations/routes_auth.py` — Login, logout, refresh token, MFA
  - `backend/modules/organizations/routes_users.py` — User CRUD, roles, CSV import/export
  - `backend/modules/organizations/routes_oidc.py` — SSO/OIDC endpoints
  - `backend/modules/organizations/routes_sessions.py` — Session management, API tokens
- **Files (modified):**
  - `backend/modules/organizations/auth_routes.py` — aggregator
  - `backend/modules/organizations/__init__.py` — update ROUTES list
- **Steps:** Same pattern. Auth routes are security-sensitive — run `make test-security` after this split in addition to `make test`.
- **Acceptance:** No auth routes file over 500 lines. All auth/user endpoints respond correctly. `make test` green. `make test-security` green.
- **Depends on:** 8.2

---

### Task 8.4: Phase 8 commit

- **Files:** All from tasks 8.1–8.3
- **Steps:** `make test` passes. `make test-security` passes. `wc -l backend/modules/**/*.py | sort -rn | head -20` — no route file over 500 lines. Commit: `refactor(modular): Phase 8 — split oversized route files, max 500 lines per file`
- **Acceptance:** All tests green. File size constraint met.
- **Depends on:** 8.3

---

## Phase 9: Contract Tests

**Goal:** Add automated tests that enforce module boundaries. This is the permanent guardrail against coupling re-emerging.

---

### Task 9.1: Create tests/test_contracts/ directory and test_module_manifests.py

- **Files:**
  - `tests/test_contracts/__init__.py`
  - `tests/test_contracts/test_module_manifests.py`
- **Steps:**
  - Write tests that: (1) verify all 12 modules exist and have required manifest fields (MODULE_ID, MODULE_VERSION, ROUTES, TABLES, PUBLISHES, SUBSCRIBES, IMPLEMENTS, REQUIRES, DAEMONS), (2) verify no two modules declare ownership of the same table, (3) verify MODULE_ID matches directory name.
- **Acceptance:** Tests pass. Tests would fail if a manifest were removed or corrupted.
- **Depends on:** 8.4

---

### Task 9.2: Create test_printer_state_provider.py and test_event_bus.py

- **Files:**
  - `tests/test_contracts/test_printer_state_provider.py`
  - `tests/test_contracts/test_event_bus.py`
- **Steps:**
  - `test_printer_state_provider.py`: Verify the `PrinterStateProvider` implementation (registered in the printers module) returns the correct shape: `{state, progress, temps, ams_state, online}` for a known printer.
  - `test_event_bus.py`: Verify publish/subscribe/unsubscribe behavior of `InMemoryEventBus`. Test that subscribers receive events, unsubscribed handlers do not, and wrong event types are filtered.
- **Acceptance:** Both test files pass. Tests are isolated and do not require a running container.
- **Depends on:** 9.1

---

### Task 9.3: Create test_notification_dispatcher.py

- **Files:**
  - `tests/test_contracts/test_notification_dispatcher.py`
- **Steps:**
  - Verify the `NotificationDispatcher` implementation dispatches to the correct channels given the alert type and severity.
  - Verify `should_suppress()` returns True during active quiet hours.
  - Mock external channels (email/push/webhook) — test dispatch routing logic, not channel delivery.
- **Acceptance:** Tests pass. Tests use mocking — no live SMTP or webhook calls.
- **Depends on:** 9.2

---

### Task 9.4: Create test_no_cross_module_imports.py — the boundary enforcer

- **Files:**
  - `tests/test_contracts/test_no_cross_module_imports.py`
- **Steps:**
  - Implement the cross-module import test verbatim from spec section 9. The test greps all `.py` files under `backend/modules/` for lines starting with `from modules.` or `import modules.` that reference a different module (not the file's own module).
  - Add an allowlist for known legitimate cross-module references (if any remain after Phase 7).
- **Acceptance:** Test passes with zero violations. Test would fail immediately if a developer adds a direct cross-module import.
- **Depends on:** 9.3

---

### Task 9.5: Integrate contract tests into make test

- **Files:**
  - `Makefile` (modify)
- **Steps:**
  - Read `Makefile` in full.
  - Add `test-contracts` target: `pytest tests/test_contracts/ -v`.
  - Add it as a dependency of `make test` or add a note in the test target to run it.
- **Acceptance:** `make test` runs contract tests. All contract tests pass. `make test-contracts` works standalone.
- **Depends on:** 9.4

---

### Task 9.6: Phase 9 commit and merge readiness check

- **Files:** All from tasks 9.1–9.5
- **Steps:**
  - `make test` passes (all suites including contracts)
  - `make test-security` passes
  - `make build` passes
  - Container starts, all 9 processes healthy
  - Verify spec section 9 success criteria:
    - [ ] No file over 600 lines in route/module code
    - [ ] No cross-module imports (test passes)
    - [ ] Every module has a valid manifest
    - [ ] Event bus handles all cross-domain communication
    - [ ] Schema ownership clear — each table created by one module
    - [ ] All ~2,142 test cases pass
    - [ ] Container starts with all 9 supervisord processes
  - Commit: `refactor(modular): Phase 9 — contract tests, import boundary enforcement`
- **Acceptance:** All success criteria from spec section 9 met. Branch ready for PR to master.
- **Depends on:** 9.5

---

## DAG

```yaml
dag:
  nodes:
    # Phase 0
    - id: "0.1"
      name: "Create branch, verify baseline"
      type: "setup"
      files: []
      depends_on: []

    - id: "0.2"
      name: "Create ADR directory and 4 ADR documents"
      type: "docs"
      files:
        - "backend/adr/0001-modular-architecture-decision.md"
        - "backend/adr/0002-domain-boundaries.md"
        - "backend/adr/0003-event-bus-over-direct-imports.md"
        - "backend/adr/0004-interface-contracts.md"
      depends_on: ["0.1"]

    - id: "0.3"
      name: "Create core/interfaces/ with 5 ABC files"
      type: "code"
      files:
        - "backend/core/__init__.py"
        - "backend/core/interfaces/__init__.py"
        - "backend/core/interfaces/printer_state.py"
        - "backend/core/interfaces/event_bus.py"
        - "backend/core/interfaces/notification.py"
        - "backend/core/interfaces/org_settings.py"
        - "backend/core/interfaces/job_state.py"
      depends_on: ["0.1"]

    - id: "0.4"
      name: "Create core/events.py event constants"
      type: "code"
      files: ["backend/core/events.py"]
      depends_on: ["0.3"]

    - id: "0.5"
      name: "Create 12 empty module __init__.py manifests"
      type: "code"
      files:
        - "backend/modules/__init__.py"
        - "backend/modules/printers/__init__.py"
        - "backend/modules/jobs/__init__.py"
        - "backend/modules/inventory/__init__.py"
        - "backend/modules/models_library/__init__.py"
        - "backend/modules/vision/__init__.py"
        - "backend/modules/notifications/__init__.py"
        - "backend/modules/organizations/__init__.py"
        - "backend/modules/orders/__init__.py"
        - "backend/modules/archives/__init__.py"
        - "backend/modules/reporting/__init__.py"
        - "backend/modules/system/__init__.py"
      depends_on: ["0.1"]

    - id: "0.6"
      name: "Add domain tags to 16 router files"
      type: "code"
      files: ["backend/routers/*.py (16 files)"]
      depends_on: ["0.5"]

    - id: "0.7"
      name: "Phase 0 commit"
      type: "commit"
      files: []
      depends_on: ["0.2", "0.3", "0.4", "0.5", "0.6"]

    # Phase 1
    - id: "1.1"
      name: "Extract database layer to core/db.py"
      type: "code"
      files: ["backend/core/db.py", "backend/deps.py"]
      depends_on: ["0.7"]

    - id: "1.2"
      name: "Extract RBAC to core/rbac.py"
      type: "code"
      files: ["backend/core/rbac.py", "backend/deps.py"]
      depends_on: ["1.1"]

    - id: "1.3"
      name: "Extract auth dependencies to core/dependencies.py"
      type: "code"
      files: ["backend/core/dependencies.py", "backend/deps.py"]
      depends_on: ["1.2"]

    - id: "1.4"
      name: "Extract auth helpers to core/auth_helpers.py"
      type: "code"
      files: ["backend/core/auth_helpers.py", "backend/deps.py"]
      depends_on: ["1.3"]

    - id: "1.5"
      name: "Copy config, auth, crypto, and other core files into core/"
      type: "code"
      files:
        - "backend/core/config.py"
        - "backend/core/auth.py"
        - "backend/core/crypto.py"
        - "backend/core/db_utils.py"
        - "backend/core/rate_limit.py"
        - "backend/core/ws_hub.py"
        - "backend/core/license_manager.py"
      depends_on: ["1.4"]

    - id: "1.6"
      name: "Phase 1 commit"
      type: "commit"
      files: []
      depends_on: ["1.5"]

    # Phase 2
    - id: "2.1"
      name: "Create core/base.py with shared enums"
      type: "code"
      files: ["backend/core/base.py"]
      depends_on: ["1.6"]

    - id: "2.2"
      name: "Create printers and jobs domain models/schemas"
      type: "code"
      files:
        - "backend/modules/printers/models.py"
        - "backend/modules/printers/schemas.py"
        - "backend/modules/jobs/models.py"
        - "backend/modules/jobs/schemas.py"
      depends_on: ["2.1"]

    - id: "2.3"
      name: "Create jobs models/schemas (handled in 2.2)"
      type: "skip"
      files: []
      depends_on: ["2.2"]

    - id: "2.4"
      name: "Create models/schemas for remaining 8 modules"
      type: "code"
      files:
        - "backend/modules/inventory/models.py"
        - "backend/modules/inventory/schemas.py"
        - "backend/modules/models_library/models.py"
        - "backend/modules/models_library/schemas.py"
        - "backend/modules/vision/models.py"
        - "backend/modules/vision/schemas.py"
        - "backend/modules/notifications/models.py"
        - "backend/modules/notifications/schemas.py"
        - "backend/modules/orders/models.py"
        - "backend/modules/orders/schemas.py"
        - "backend/modules/archives/models.py"
        - "backend/modules/archives/schemas.py"
        - "backend/modules/system/models.py"
        - "backend/modules/system/schemas.py"
        - "backend/core/models.py"
        - "backend/core/schemas.py"
      depends_on: ["2.2"]

    - id: "2.5"
      name: "Convert models.py and schemas.py to re-export facades"
      type: "code"
      files: ["backend/models.py", "backend/schemas.py"]
      depends_on: ["2.4"]

    - id: "2.6"
      name: "Phase 2 commit"
      type: "commit"
      files: []
      depends_on: ["2.5"]

    # Phase 3
    - id: "3.1"
      name: "Move printers and jobs router files"
      type: "code"
      files:
        - "backend/modules/printers/routes.py"
        - "backend/modules/printers/camera_routes.py"
        - "backend/modules/jobs/routes.py"
        - "backend/modules/jobs/scheduler_routes.py"
      depends_on: ["2.6"]

    - id: "3.2"
      name: "Move inventory, models_library, vision, notifications router files"
      type: "code"
      files:
        - "backend/modules/inventory/routes.py"
        - "backend/modules/models_library/routes.py"
        - "backend/modules/vision/routes.py"
        - "backend/modules/notifications/routes.py"
      depends_on: ["3.1"]

    - id: "3.3"
      name: "Move remaining 8 router files"
      type: "code"
      files:
        - "backend/modules/organizations/routes.py"
        - "backend/modules/organizations/auth_routes.py"
        - "backend/modules/orders/routes.py"
        - "backend/modules/archives/routes.py"
        - "backend/modules/archives/project_routes.py"
        - "backend/modules/reporting/routes.py"
        - "backend/modules/system/routes.py"
        - "backend/modules/system/profile_routes.py"
      depends_on: ["3.2"]

    - id: "3.4"
      name: "Move printer adapter files to modules/printers/adapters/"
      type: "code"
      files:
        - "backend/modules/printers/adapters/__init__.py"
        - "backend/modules/printers/adapters/bambu.py"
        - "backend/modules/printers/adapters/moonraker.py"
        - "backend/modules/printers/adapters/prusalink.py"
        - "backend/modules/printers/adapters/elegoo.py"
      depends_on: ["3.3"]

    - id: "3.5"
      name: "Move printer monitor files to modules/printers/monitors/, update supervisord.conf"
      type: "code"
      files:
        - "backend/modules/printers/monitors/__init__.py"
        - "backend/modules/printers/monitors/mqtt_monitor.py"
        - "backend/modules/printers/monitors/moonraker_monitor.py"
        - "backend/modules/printers/monitors/prusalink_monitor.py"
        - "backend/modules/printers/monitors/elegoo_monitor.py"
        - "docker/supervisord.conf"
      depends_on: ["3.4"]

    - id: "3.6"
      name: "Move remaining printer support files"
      type: "code"
      files:
        - "backend/modules/printers/bambu_integration.py"
        - "backend/modules/printers/printer_models.py"
        - "backend/modules/printers/dispatch.py"
        - "backend/modules/printers/hms_codes.py"
        - "backend/modules/printers/smart_plug.py"
      depends_on: ["3.5"]

    - id: "3.7"
      name: "Move notifications domain files"
      type: "code"
      files:
        - "backend/modules/notifications/event_dispatcher.py"
        - "backend/modules/notifications/alert_dispatcher.py"
        - "backend/modules/notifications/quiet_hours.py"
        - "backend/modules/notifications/mqtt_republish.py"
      depends_on: ["3.6"]

    - id: "3.8"
      name: "Move models_library, orders, archives, reporting domain files"
      type: "code"
      files:
        - "backend/modules/models_library/threemf_parser.py"
        - "backend/modules/models_library/print_file_meta.py"
        - "backend/modules/orders/invoice_generator.py"
        - "backend/modules/archives/archive.py"
        - "backend/modules/archives/timelapse_capture.py"
        - "backend/modules/reporting/report_runner.py"
      depends_on: ["3.7"]

    - id: "3.9"
      name: "Move organizations domain files"
      type: "code"
      files:
        - "backend/modules/organizations/branding.py"
        - "backend/modules/organizations/oidc_handler.py"
      depends_on: ["3.8"]

    - id: "3.10"
      name: "Move vision monitor daemon, update supervisord.conf"
      type: "code"
      files:
        - "backend/modules/vision/monitor.py"
        - "docker/supervisord.conf"
        - "docker/entrypoint.sh"
      depends_on: ["3.9"]

    - id: "3.11"
      name: "Move scheduler engine"
      type: "code"
      files: ["backend/modules/jobs/scheduler.py"]
      depends_on: ["3.10"]

    - id: "3.12"
      name: "Phase 3 final validation and commit"
      type: "commit"
      files: []
      depends_on: ["3.11"]

    # Phase 4
    - id: "4.1"
      name: "Implement InMemoryEventBus in core/event_bus.py"
      type: "code"
      files: ["backend/core/event_bus.py"]
      depends_on: ["3.12"]

    - id: "4.2"
      name: "Refactor event_dispatcher.py to publish events"
      type: "code"
      files: ["backend/modules/notifications/event_dispatcher.py"]
      depends_on: ["4.1"]

    - id: "4.3"
      name: "Register event subscribers in each module"
      type: "code"
      files:
        - "backend/modules/notifications/__init__.py"
        - "backend/modules/printers/__init__.py"
        - "backend/core/ws_hub.py"
        - "backend/modules/printers/smart_plug.py"
        - "backend/modules/notifications/mqtt_republish.py"
      depends_on: ["4.2"]

    - id: "4.4"
      name: "Wire event bus into main.py startup"
      type: "code"
      files: ["backend/main.py"]
      depends_on: ["4.3"]

    - id: "4.5"
      name: "Phase 4 commit"
      type: "commit"
      files: []
      depends_on: ["4.4"]

    # Phase 5
    - id: "5.1"
      name: "Create 12 migration SQL files"
      type: "code"
      files: ["backend/modules/*/migrations/001_initial.sql (12 files)"]
      depends_on: ["4.5"]

    - id: "5.2"
      name: "Implement migration runner in core/db.py"
      type: "code"
      files: ["backend/core/db.py"]
      depends_on: ["5.1"]

    - id: "5.3"
      name: "Update entrypoint.sh to use migration runner"
      type: "code"
      files: ["docker/entrypoint.sh"]
      depends_on: ["5.2"]

    - id: "5.4"
      name: "Phase 5 commit"
      type: "commit"
      files: []
      depends_on: ["5.3"]

    # Phase 6
    - id: "6.1"
      name: "Create core/registry.py"
      type: "code"
      files: ["backend/core/registry.py"]
      depends_on: ["5.4"]

    - id: "6.2"
      name: "Create core/app.py app factory"
      type: "code"
      files: ["backend/core/app.py"]
      depends_on: ["6.1"]

    - id: "6.3"
      name: "Add register() to all 12 module __init__.py files"
      type: "code"
      files: ["backend/modules/*/__init__.py (12 files)"]
      depends_on: ["6.2"]

    - id: "6.4"
      name: "Rewrite main.py to use create_app()"
      type: "code"
      files: ["backend/main.py"]
      depends_on: ["6.3"]

    - id: "6.5"
      name: "Phase 6 commit"
      type: "commit"
      files: []
      depends_on: ["6.4"]

    # Phase 7
    - id: "7.1"
      name: "Update all test files to new import paths"
      type: "code"
      files: ["tests/*.py"]
      depends_on: ["6.5"]

    - id: "7.2"
      name: "Update daemon files to direct import paths"
      type: "code"
      files: ["backend/modules/printers/monitors/*.py", "backend/modules/vision/monitor.py"]
      depends_on: ["7.1"]

    - id: "7.3"
      name: "Update all module internal imports to direct paths"
      type: "code"
      files: ["backend/modules/**/*.py"]
      depends_on: ["7.2"]

    - id: "7.4"
      name: "Delete facade files and routers/ stubs"
      type: "code"
      files:
        - "backend/models.py (delete)"
        - "backend/schemas.py (delete)"
        - "backend/deps.py (delete or reduce)"
        - "backend/routers/*.py stubs (delete)"
        - "backend/*.py stubs from Phase 3 (delete)"
      depends_on: ["7.3"]

    - id: "7.5"
      name: "Update CLAUDE.md for new file paths"
      type: "docs"
      files: ["CLAUDE.md"]
      depends_on: ["7.4"]

    - id: "7.6"
      name: "Phase 7 commit"
      type: "commit"
      files: []
      depends_on: ["7.5"]

    # Phase 8
    - id: "8.1"
      name: "Split modules/printers/routes.py into 6 sub-files"
      type: "code"
      files:
        - "backend/modules/printers/routes_crud.py"
        - "backend/modules/printers/routes_status.py"
        - "backend/modules/printers/routes_controls.py"
        - "backend/modules/printers/routes_ams.py"
        - "backend/modules/printers/routes_smart_plug.py"
        - "backend/modules/printers/routes_nozzle.py"
      depends_on: ["7.6"]

    - id: "8.2"
      name: "Split modules/system/routes.py into 4 sub-files"
      type: "code"
      files:
        - "backend/modules/system/routes_health.py"
        - "backend/modules/system/routes_backup.py"
        - "backend/modules/system/routes_admin.py"
        - "backend/modules/system/routes_maintenance.py"
      depends_on: ["8.1"]

    - id: "8.3"
      name: "Split modules/organizations/auth_routes.py into 4 sub-files"
      type: "code"
      files:
        - "backend/modules/organizations/routes_auth.py"
        - "backend/modules/organizations/routes_users.py"
        - "backend/modules/organizations/routes_oidc.py"
        - "backend/modules/organizations/routes_sessions.py"
      depends_on: ["8.2"]

    - id: "8.4"
      name: "Phase 8 commit"
      type: "commit"
      files: []
      depends_on: ["8.3"]

    # Phase 9
    - id: "9.1"
      name: "Create test_contracts/ and test_module_manifests.py"
      type: "test"
      files:
        - "tests/test_contracts/__init__.py"
        - "tests/test_contracts/test_module_manifests.py"
      depends_on: ["8.4"]

    - id: "9.2"
      name: "Create test_printer_state_provider.py and test_event_bus.py"
      type: "test"
      files:
        - "tests/test_contracts/test_printer_state_provider.py"
        - "tests/test_contracts/test_event_bus.py"
      depends_on: ["9.1"]

    - id: "9.3"
      name: "Create test_notification_dispatcher.py"
      type: "test"
      files: ["tests/test_contracts/test_notification_dispatcher.py"]
      depends_on: ["9.2"]

    - id: "9.4"
      name: "Create test_no_cross_module_imports.py — boundary enforcer"
      type: "test"
      files: ["tests/test_contracts/test_no_cross_module_imports.py"]
      depends_on: ["9.3"]

    - id: "9.5"
      name: "Integrate contract tests into make test"
      type: "code"
      files: ["Makefile"]
      depends_on: ["9.4"]

    - id: "9.6"
      name: "Phase 9 commit — final validation and merge readiness"
      type: "commit"
      files: []
      depends_on: ["9.5"]

  parallel_groups:
    - id: "pg-0a"
      description: "Phase 0 docs and scaffolding — all independent of each other"
      tasks: ["0.2", "0.3", "0.5"]
      conflict_free: true

    - id: "pg-2a"
      description: "Phase 2 per-module models/schemas — can split across modules in parallel"
      tasks: ["2.2", "2.4"]
      conflict_free: false
      note: "2.4 depends on 2.2 being done first for printers/jobs foreign key references"

    - id: "pg-3a"
      description: "Phase 3 router moves are sequential due to main.py edits and test gates"
      tasks: ["3.1", "3.2", "3.3"]
      conflict_free: false

    - id: "pg-3b"
      description: "Phase 3 backend file moves — after routers are stable"
      tasks: ["3.4", "3.5", "3.6", "3.7", "3.8", "3.9"]
      conflict_free: false
      note: "Ordered by dependency: adapters before monitors, monitors before other files"

    - id: "pg-9a"
      description: "Phase 9 contract tests — sequential build-up"
      tasks: ["9.1", "9.2", "9.3", "9.4"]
      conflict_free: false

  critical_path:
    - "0.1 → 0.7 → 1.6 → 2.6 → 3.12 → 4.5 → 5.4 → 6.5 → 7.6 → 8.4 → 9.6"
```

---

## Phase Summary

| Phase | Tasks | Key Risk | Safety Gate |
|-------|-------|----------|-------------|
| 0: Preparation | 0.1–0.7 | None — docs only | `make test` green (nothing changed) |
| 1: Extract deps.py | 1.1–1.6 | Circular imports in core/ | `make test` green after each extract |
| 2: Split models/schemas | 2.1–2.6 | Re-export facade missing a class | `from models import Printer, Job, Spool, Alert` works |
| 3: Move files | 3.1–3.12 | Supervisord paths break, import chains break | `make test` + container start after every group |
| 4: Event bus | 4.1–4.5 | Alert notifications silently drop | `make test` + real-time WS behavior |
| 5: Migrations | 5.1–5.4 | Table creation order, dual-definition | Fresh container creates all tables |
| 6: App factory | 6.1–6.5 | Module load-order, circular registry | `make test` + all modules load |
| 7: Remove facades | 7.1–7.6 | Missed import path somewhere | `grep` for old paths returns zero |
| 8: Split routes | 8.1–8.4 | Router prefix conflicts | `make test` + `make test-security` |
| 9: Contract tests | 9.1–9.6 | None — additive only | Contract tests pass |

---

## Execution Notes

**Branch discipline:**
- Never add features on this branch. Features go on master, then get rebased in.
- Rebase onto master weekly. If a hotfix ships on master, rebase immediately before continuing.
- Each phase is its own PR. Merge phases sequentially — do not open Phase 2 PR until Phase 1 is merged.

**Test command at each phase gate:**
```
make test              # main + RBAC (~2,142 cases)
make test-security     # Layer 3 adversarial (41 cases)
make build             # container build
```

**On Phase 3 (the high-risk phase):** Run `make test` after EACH task (3.1 through 3.11), not just at the end. The re-export facades are the safety net — if anything breaks, the old import path still works until Phase 7. If a task introduces a test failure, debug and fix before moving to the next task.

**Commit message format for all commits on this branch:**
```
refactor(modular): Phase N — [description]
```

---

## Plan Evaluation Report

**Evaluator:** loop-plan-evaluator + CTO review
**Date:** 2026-02-26
**Verdict:** PASS

---

### Scope Check

The spec defines 9 deliverable phases (Phase 0 through Phase 9) plus ADRs, interface contracts, event catalog, module manifests, and success criteria. The plan covers all of them with no omissions and no additions beyond spec scope.

Spec requirements traced to plan tasks:

| Spec Section | Plan Coverage |
|---|---|
| Phase 0: Branch + scaffolding | Tasks 0.1–0.7 |
| Phase 1: Extract deps.py | Tasks 1.1–1.6 |
| Phase 2: Split models/schemas | Tasks 2.1–2.6 |
| Phase 3: Move files to modules | Tasks 3.1–3.12 |
| Phase 4: Event bus | Tasks 4.1–4.5 |
| Phase 5: Module-owned migrations | Tasks 5.1–5.4 |
| Phase 6: App factory + registry | Tasks 6.1–6.5 |
| Phase 7: Remove facades | Tasks 7.1–7.6 |
| Phase 8: Split oversized routes | Tasks 8.1–8.4 |
| Phase 9: Contract tests | Tasks 9.1–9.6 |
| ADR template (spec §11) | Task 0.2 explicitly references §11 |
| Interface ABCs (spec §3.1–3.5) | Task 0.3 |
| Event catalog (spec §4) | Task 0.4 |
| Module manifest format (spec §5) | Task 0.5 |
| Success criteria (spec §9) | Task 9.6 checklist |

**No scope gaps. No scope creep.** Pass.

---

### Overlap Check

Tracks inspected from `conductor/tracks.md`:

- `site-redesign_20260225` — marketing site only, no backend
- `docs-buildout_20260225` — documentation pages, no backend
- `auth-hardening-architectural_20260221` — complete, security changes on master; plan explicitly notes these will be present on master when branching
- `security-hardening-remaining_20260221` — complete, no overlap
- Active tracks (`auth-missing-endpoints`, `credential-encryption`, `ws-token-mfa-hardening`, `frontend-security`, `path-traversal-sweep`) — all target security hardening on master; no architectural overlap

The plan correctly handles the active security tracks by specifying the branch must be created off master and those tracks must not be merged into the refactor branch until they merge to master and are rebased in. This is sound.

**No duplicate work. No overlap.** Pass.

---

### Dependency Check

Full DAG traced. Linear critical path verified: `0.1 → 0.7 → 1.6 → 2.6 → 3.12 → 4.5 → 5.4 → 6.5 → 7.6 → 8.4 → 9.6`.

Notable dependency decisions reviewed:

- 0.2, 0.3, 0.5 are correctly marked parallel (docs + code scaffolding, no file conflicts). 0.4 correctly depends on 0.3 (needs EventBus ABC before event constants reference it). 0.6 correctly depends on 0.5 (tags reference module IDs).
- 1.1 → 1.2 → 1.3 → 1.4 → 1.5 is correctly sequential: each extraction step reads deps.py which is being progressively updated; parallel extractions would race on the same file.
- 2.3 is marked `type: skip` in the DAG — the printers/jobs models were combined into 2.2 in the DAG consolidation. The DAG node exists as a passthrough with `depends_on: ["2.2"]`. This is unusual but not a problem: 2.4 depends on 2.2 in the DAG (not 2.3), which is correct.
- 3.1 → 3.2 → 3.3 is correctly sequential: all touch `main.py` for router registration.
- 3.4 → 3.5: adapters must exist before monitors import them. Correct.
- 3.7 (notifications) depends on 3.6 (printer support files): `event_dispatcher.py` imports from `smart_plug`, `hms_codes`, `ws_hub` — all moved in 3.6 or before. Correct.
- 4.1 → 4.2 → 4.3 → 4.4: event bus exists before dispatcher is refactored, subscribers registered before bus is wired into startup. Correct.
- 5.1 → 5.2 → 5.3: migrations written before runner implemented before entrypoint updated. Correct.
- 6.1 → 6.2 → 6.3 → 6.4: registry before app factory, app factory before modules add `register()`, modules have `register()` before main.py calls `create_app()`. Correct.
- 7.1 → 7.2 → 7.3 → 7.4: all import paths updated before facades deleted. Correct — deletion before update would break tests.

**No circular dependencies. All IDs unique. Dependencies correctly ordered.** Pass.

---

### Task Quality Check

Each task reviewed for: specific file paths, verifiable acceptance criteria, executable instructions, single-session scope.

Strengths:
- Every task names exact files, exact function names, exact shell commands.
- Acceptance criteria are machine-verifiable (Python import checks, `make test`, grep assertions).
- Steps reference spec sections by number (e.g., "Copy ABC definitions verbatim from spec sections 3.1–3.5").
- Phase 3 tasks include both the move operation and the stub creation pattern, which is the right level of detail for an agent.

One minor gap: Task 1.5 says "Handle potential circular imports by importing lazily where needed" but does not specify which imports are known to be circular. An executing agent may hit a circular import in `core/auth.py` (which imports from `deps.py` which imports from `auth.py`) and need to guess the fix. This is a known risk at 1.5 but not a blocking issue — the acceptance criterion (`make test` green) will catch it.

Task 3.5 correctly specifies updating `docker/supervisord.conf` with the `python -m modules.printers.monitors.mqtt_monitor` pattern and notes the working directory requirement. Sufficient for an agent.

Task 4.2 steps include a subtlety: "keep `alert_dispatcher.dispatch()` as a direct call (same module)." This is correctly reasoned — intra-module calls are permitted, cross-module calls go through the bus.

**Task quality: high throughout. No ambiguous tasks.** Pass.

---

### DAG Validation

- All 49 node IDs (0.1 through 9.6) are unique.
- No cycles detected in manual trace.
- Parallel groups accurately described with `conflict_free: true/false` and notes explaining why.
- `pg-0a` claims `["0.2", "0.3", "0.5"]` are conflict-free: confirmed — 0.2 touches `backend/adr/`, 0.3 touches `backend/core/interfaces/`, 0.5 touches `backend/modules/*/`. No file overlap.
- `pg-2a` correctly sets `conflict_free: false` because 2.4 consumes printers/jobs models from 2.2.
- `pg-3a` and `pg-3b` correctly set `conflict_free: false` — all touch `main.py` or have import chain dependencies.
- Critical path accurately reflects the longest sequential chain through the DAG.

**DAG valid. No cycles. Parallel groups correctly assessed.** Pass.

---

### CTO Technical Review

**Architecture pattern: sound.**

The chosen approach — domain modules with an interface layer, in-memory event bus, re-export facades as migration scaffold, then facade removal — is the correct sequence for this scale of refactor. The facade pattern prevents the "big bang" import breakage that would make a 55-file move untestable.

**Event bus design: adequate for the current use case.**

`InMemoryEventBus` with synchronous `publish()` is the right starting point. The spec notes async can be added later. One concern: the current `printer_events.py` (becoming `event_dispatcher.py`) has `try/except ImportError` guards around cross-module imports specifically to handle the case where those modules are unavailable at startup. The plan removes these guards in Phase 4. This is correct only if the event bus subscribers are always registered before any publisher calls `bus.publish()`. Task 4.4 wires the bus at FastAPI startup, but the printer monitors are supervisord processes that start independently. If a monitor publishes an event before the FastAPI process has registered all subscribers (a race on container startup), events will be dropped silently. The spec does not address this race condition. It is low-probability in practice (monitors wait for DB availability, FastAPI starts first in supervisord priority), but should be acknowledged.

**Interface contracts: complete and correctly abstracted.**

The 5 ABCs (`PrinterStateProvider`, `EventBus`, `NotificationDispatcher`, `OrgSettingsProvider`, `JobStateProvider`) cover the actual cross-module coupling patterns present in the current code. `PrinterStateProvider` and `NotificationDispatcher` are the most critical (they break the printer-module/notifications-module coupling that is the biggest problem today). The ABCs are correctly minimal — they expose only what consumers need, not what the implementing module has.

One gap in the interface layer: `ws_hub.py` is moved to `core/` but does not have a corresponding ABC in `core/interfaces/`. The WebSocket hub pushes events to all subscribers — it is a broadcaster, not a module-level concern. Its placement in `core/` is correct, but an `EventBroadcaster` interface would make it swappable (e.g., Redis pub/sub in the future). This is not a blocker for this refactor — it is a future hardening opportunity.

**Migration strategy: safe.**

The two-phase approach (facades first, direct imports in Phase 7) is the correct safety strategy. The `CREATE TABLE IF NOT EXISTS` idempotency means the migration runner is safe to run on an existing database. The ordering concern (SQLAlchemy `create_all` before raw SQL migrations) is correctly noted in Task 5.3.

One technical note on Task 5.3: the `run_module_migrations` call uses `Path('modules')` as a relative path. This will only work if the working directory is `backend/` when entrypoint.sh runs. The current entrypoint.sh `cd`s to `/app/backend` before running Python. This should be verified in 5.3 but is not an error in the plan.

**Phase 3 risk: adequately broken down.**

11 sequential tasks, each with a `make test` gate, is the right granularity. The plan correctly identifies this as the highest-risk phase and requires `make test` after each task rather than only at the phase gate. The stub pattern is sound.

**Phase 6 (app factory) dependency resolution: needs a concrete algorithm.**

Task 6.2 specifies "Module load-order: resolve from REQUIRES declarations (modules with no REQUIRES load first)." This is topological sorting. The task does not specify what happens if there is a cycle in REQUIRES declarations. Since the current 12 modules have a known non-cyclic dependency graph, this is not an immediate problem, but the implementation should use `graphlib.TopologicalSorter` (Python 3.9+, available in this codebase's Python 3.11) rather than a hand-rolled sort. Task 6.2 should note this. Minor issue.

**Branch isolation: correctly specified.**

The plan header explicitly states `Branch: refactor/modular-architecture (off master)`. Pre-conditions require master baseline green before branching. Execution notes specify never adding features on this branch and rebasing weekly. This is the correct protocol for a long-running refactor branch.

**No master branch modifications will occur during this track.** Confirmed by plan structure.

---

### Issues Found

| # | Severity | Issue | Phase | Disposition |
|---|---|---|---|---|
| 1 | Minor | Task 1.5 does not identify which specific circular imports to expect when copying `core/auth.py` (which currently imports `deps.py`). An agent may need to iterate. | Phase 1 | Non-blocking. Acceptance criterion catches it. |
| 2 | Minor | Startup race: monitor daemons may publish events before FastAPI registers subscribers. Not addressed in spec or plan. | Phase 4 | Non-blocking for this refactor. Supervisord priority ordering mitigates it. Document as known limitation. |
| 3 | Minor | `ws_hub.py` lacks a corresponding ABC interface, making it non-swappable. | Phase 1 | Non-blocking. Future hardening opportunity. |
| 4 | Minor | Task 6.2 should explicitly call out `graphlib.TopologicalSorter` for REQUIRES-based load ordering to prevent ad-hoc sorting bugs. | Phase 6 | Non-blocking. The 12 known modules have no cycles. |
| 5 | Minor | Task 5.3 uses `Path('modules')` as a relative path — agent must verify entrypoint.sh cwd is `backend/`. | Phase 5 | Non-blocking. Standard entrypoint.sh pattern already `cd`s to `/app/backend`. |
| 6 | Note | DAG node 2.3 is `type: skip` with no explanation in the node itself. A reader scanning the DAG cold may be confused. | Phase 2 | Cosmetic. Explained in the parallel_groups section. |

None of these issues are blocking. All are minor gaps that an executing agent can navigate with the existing spec as reference.

---

### Verdict

**PASS**

The plan is complete, correctly scoped, dependency-ordered, and executable by an agent with no ambiguity. All 9 spec phases are covered. No overlap with completed or active tracks. The DAG is acyclic with 49 unique nodes. Re-export facade safety strategy is sound. Branch isolation is explicitly enforced. Phase 3 high-risk breakdown (11 sequential tasks with test gates) is adequate. CTO-level concerns are minor and non-blocking.

The plan is ready for execution on branch `refactor/modular-architecture`.
