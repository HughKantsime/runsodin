# ADR-001: Modular Architecture

**Date:** 2026-02-26
**Status:** Accepted
**Versions:** v1.4.0 (module split), v1.4.1 (route sub-router decomposition)

## Context

The backend started as a monolith: `main.py` (524 lines of route registration), `entrypoint.sh` (960 lines of SQL DDL), and 13 flat router files under `routers/` — some exceeding 1,300 lines. There were no import boundaries, no interface contracts, and no way to reason about which code owned which domain.

As the feature set grew to 454 features across 22 sections, this structure became a liability. Changes in one domain (e.g., adding an alert type) required editing files across the codebase. Tests couldn't verify isolation. New developers (human or AI) had to hold the entire backend in context to make safe changes.

## Decision

Decompose the backend into **12 self-contained domain modules** with strict boundaries, interface-based communication, and automated enforcement.

### Module structure

```
backend/modules/{name}/
├── __init__.py          # Manifest: MODULE_ID, ROUTES, TABLES, PUBLISHES, etc.
├── models.py            # SQLAlchemy ORM models
├── schemas.py           # Pydantic request/response schemas
├── routes/              # Sub-router package
│   ├── __init__.py      # Assembles and exports combined `router`
│   ├── {domain_a}.py    # Focused sub-router (~150-300 lines)
│   └── {domain_b}.py
└── migrations/
    └── 001_initial.sql  # Module-owned DDL
```

### The 12 modules

`core` (shared kernel), `printers`, `jobs`, `inventory`, `models_library`, `vision`, `notifications`, `organizations`, `orders`, `archives`, `reporting`, `system`

### Rules

1. **600-line limit** — No single `.py` file under `modules/` may exceed 600 lines. When a file approaches this limit, split it.

2. **No cross-module route imports** — Modules must not import route handlers or service functions from another module's `routes/` files. Use interfaces (`core/interfaces/`) or events (`core/events.py`) instead.

3. **Routes are packages, not files** — Every module with endpoints uses a `routes/` directory containing focused sub-router files, not a monolithic `routes.py`.

4. **Manifests are mandatory** — Every module's `__init__.py` declares `MODULE_ID`, `ROUTES`, `TABLES`, `PUBLISHES`, `SUBSCRIBES`, `IMPLEMENTS`, `REQUIRES`, `DAEMONS`, and a `register(app, registry)` function.

5. **Shared types are allowed** — Importing `models.py` and `schemas.py` from other modules is permitted (read-only data types). Importing route logic is not.

6. **core/ has no routes** — The `core/` package is the shared kernel: interfaces, event bus, base classes. It must not contain `APIRouter` definitions.

## Enforcement

All rules above are enforced by automated contract tests that run on every `make test-contracts`:

| Rule | Test file |
|------|-----------|
| 600-line limit | `test_architecture_lint.py::TestFileSizeLimits` |
| No cross-module route imports | `test_no_cross_module_imports.py::TestNoCrossModuleImports` |
| Routes are packages | `test_architecture_lint.py::TestRoutePackageStructure` |
| Manifests are mandatory | `test_module_manifests.py::TestManifestFields` |
| No duplicate table ownership | `test_module_manifests.py::TestTableOwnership` |
| core/ has no routes | `test_architecture_lint.py::TestCoreHasNoRoutes` |

If you need to violate a rule, the test will fail. Add an entry to the relevant allowlist with a comment explaining *why* — and plan to remove it.

## Consequences

**Good:**
- Changes to one domain don't require reading or modifying other domains
- Contract tests catch boundary violations before code is shipped
- AI-assisted development can operate on a single module without full-codebase context
- New modules follow a predictable template (manifest + routes package + models + schemas)

**Bad:**
- More files to navigate (25 route files instead of 8, ~40 total module files)
- Some cross-module needs require interface ceremony (registry lookup) instead of a direct import
- Two known violations remain (`calculate_job_cost`, `_get_org_settings`) — tracked in `KNOWN_VIOLATIONS` in `test_no_cross_module_imports.py`

## When to revisit

- If a module grows beyond 10 sub-router files, consider splitting it into two modules
- If `KNOWN_VIOLATIONS` grows beyond 5 entries, prioritize extracting a shared service layer
- If a new cross-cutting concern emerges (e.g., caching, rate limiting), add it to `core/` — not to individual modules
