# ADR-0002: Domain Boundary Definitions (12 Modules)

**Status:** accepted
**Date:** 2026-02-26
**Deciders:** Shane Smith

## Context

Having decided to adopt a modular architecture (ADR-0001), we must define the module boundaries.
The wrong boundaries create more coupling than the monolith they replace. The right boundaries
align with the actual dependency graph and the natural "what changes together" clusters.

The actual import graph as of v1.3.76 was analyzed to produce the dependency map in
`spec.md` section 1.4. The database schema ownership map (section 1.5) was also analyzed to
identify which tables naturally belong together.

Key observations from the coupling map:
- `printer_events.py` is the central hub — it imports from 6 other files and is imported by 4
  monitor daemons. This is the highest-priority file to decoupled (Phase 4).
- `deps.py` is imported by every router. It conflates DB engine setup, auth token validation,
  RBAC enforcement, org scoping, and rate limiting.
- Routers `auth.py` (1888 lines) and `system.py` (2060 lines) each contain code for 3-4 distinct
  domains, indicating misassignment in the original flat layout.
- The `orders.py` and `spools.py` routers are relatively self-contained — low coupling, easy
  boundaries.

## Decision

Define 12 domain modules based on the "what tables and files naturally belong together" analysis:

| Module | Core Files | Tables Owned |
|--------|-----------|--------------|
| `core` | config, auth, crypto, deps, ws_hub, license_manager | users, sessions, tokens, SystemConfig |
| `printers` | adapters, monitors, bambu_integration, smart_plug, hms_codes, dispatch | printers, filament_slots, telemetry |
| `jobs` | scheduler | jobs, print_jobs, print_files, scheduler_runs |
| `inventory` | (logic in router) | spools, filament_library, consumables, drying_logs |
| `models_library` | threemf_parser, print_file_meta | models, model_revisions |
| `vision` | vision_monitor | vision_detections, vision_settings, vision_models |
| `notifications` | printer_events, alert_dispatcher, quiet_hours, mqtt_republish | alerts, preferences, push_subscriptions, webhooks |
| `organizations` | branding, oidc_handler | groups, oidc tables, quotas |
| `orders` | invoice_generator | orders, order_items, products, product_components |
| `archives` | archive, timelapse_capture | print_archives, projects, timelapses |
| `reporting` | report_runner | report_schedules |
| `system` | (logic in router) | maintenance_tasks, maintenance_logs, audit_log, printer_profiles |

Router assignments mirror the module boundaries:
- `routers/printers.py` + `routers/cameras.py` → `modules/printers/`
- `routers/jobs.py` + `routers/scheduler.py` → `modules/jobs/`
- `routers/spools.py` → `modules/inventory/`
- `routers/models.py` → `modules/models_library/`
- `routers/vision.py` → `modules/vision/`
- `routers/alerts.py` → `modules/notifications/`
- `routers/orgs.py` + user-management portion of `routers/auth.py` → `modules/organizations/`
- `routers/orders.py` → `modules/orders/`
- `routers/archives.py` + `routers/projects.py` → `modules/archives/`
- `routers/analytics.py` → `modules/reporting/`
- `routers/system.py` + `routers/profiles.py` → `modules/system/`

Note: `models_library` is named to avoid collision with Python's `models` module name.

## Consequences

### Positive
- Boundaries align with the actual dependency clusters — minimal artificial coupling
- Router assignment is 1:1 with domain (with two exceptions: auth.py and system.py span domains)
- Domain tag comments added to router files in Phase 0 make the mapping visible immediately
- The `organizations` module absorbs user management from `auth.py` — recognizing that users/groups
  are org-level concerns, not authentication concerns

### Negative
- `routers/auth.py` (1888 lines) must be split across two modules: login/MFA/OIDC stays in
  the auth/core domain, while user CRUD/roles/CSV export moves to `organizations`
- `routers/system.py` (2060 lines) spans too many concerns — it will need splitting in Phase 8
- The `reporting` module is thin (only `report_runner.py` and analytics routes) — it may merge
  with `system` in a future revision

### Neutral
- The module count (12) matches the natural clusters identified in the dependency graph
- Future modules (Craft Vendor, Communities) are additive — they don't change existing boundaries
