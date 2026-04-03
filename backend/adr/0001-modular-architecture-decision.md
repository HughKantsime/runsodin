# ADR-0001: Adopt Modular Architecture

**Status:** accepted
**Date:** 2026-02-26
**Deciders:** Shane Smith

## Context

O.D.I.N. v1.3.76 has grown to ~79,550 lines across ~151 backend/frontend files. The current flat
layout places all SQLAlchemy models in a single `models.py` (1054 lines), all Pydantic schemas in
`schemas.py` (856 lines), all auth/RBAC/DB dependencies in `deps.py` (556 lines), and all
cross-module event dispatching in `printer_events.py` (1049 lines).

This layout made sense at early stages, but the codebase has reached the size where:

1. **Cognitive overhead is high.** A developer fixing a spool-weight bug must load context about
   printers, jobs, orders, and auth just to navigate the shared `models.py`.

2. **God files create blast radius.** A syntax error in `models.py` breaks the entire application.
   A change to the jobs schema requires touching the same file as a change to the printer schema.

3. **Cross-domain coupling is invisible.** `printer_events.py` imports from `mqtt_republish`,
   `ws_hub`, `smart_plug`, and `hms_codes` via try/except chains. This coupling is undocumented
   and fragile.

4. **Adding new domains is expensive.** Adding the planned Craft Vendor module requires editing
   `models.py`, `schemas.py`, `deps.py`, and `main.py` — all shared files.

5. **Testing isolation is impossible.** Tests for the vision module must import from `models.py`
   which also loads printer and order models. There is no way to test one domain in isolation.

The 12 natural domain boundaries have been identified from the dependency graph (see ADR-0002).
This ADR decides whether to reorganize around those boundaries.

## Decision

Adopt a modular architecture with 12 domain modules under `backend/modules/`. Each module owns:
- Its own `models.py` (SQLAlchemy ORM)
- Its own `schemas.py` (Pydantic)
- Its own `routes.py` (FastAPI router)
- Its own migrations SQL
- A `__init__.py` manifest declaring routes, tables, events, dependencies, and interfaces

Shared platform code moves to `backend/core/` (auth, crypto, DB, RBAC, config, license).

Cross-module communication goes through a typed event bus (see ADR-0003) and abstract interface
contracts (see ADR-0004).

The refactor uses re-export facades so the transition is zero-downtime: old import paths continue
to work until Phase 7 (cleanup). Every phase boundary must pass all ~2,142 test cases.

## Consequences

### Positive
- Each domain can be understood independently
- God files are decomposed; blast radius of any change is reduced
- Cross-module coupling is explicit, documented, and enforced by automated tests
- Adding a new module requires creating one directory with no changes to existing code (Phase 6+)
- Schema ownership is unambiguous; each table is created by exactly one module
- Domains can eventually be extracted into separate services if needed (future optionality)

### Negative
- Large upfront effort (~28-38 hours across 9 phases) with no user-visible features
- Increases file count significantly (~55+ new files in Phase 3 alone)
- Rebasing this branch against master security hotfixes requires care
- During the transition (Phases 1-6), both old and new paths exist, which is temporarily confusing
- Import paths in tests will need updates in Phase 7

### Neutral
- Frontend refactor (splitting `api.js`, grouping pages) is deferred — it's not blocking and has
  lower coupling risk than the backend
