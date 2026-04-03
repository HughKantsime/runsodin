# ADR-0004: Abstract Base Classes as Interface Contracts Between Modules

**Status:** accepted
**Date:** 2026-02-26
**Deciders:** Shane Smith

## Context

Once modules are separated (Phase 3), some modules still need to call functions in other modules
synchronously — not via events, but via direct queries. Examples:

- The scheduler (jobs module) needs to query available printers with matching filament colors.
  This is a synchronous query that must return a result before scheduling can proceed.
- The notifications module needs to read org quiet hours settings before deciding whether to
  suppress an alert. This is a per-request lookup that cannot be deferred to an event.

The event bus (ADR-0003) handles one-way fire-and-forget communication. It does not solve the
synchronous query problem.

Two patterns were considered:

1. **Direct module imports** — `from modules.printers.services import get_available_printers`.
   This creates hard coupling between modules. If the printers module changes its internal
   structure, every module that imports from it must also change. It also creates a risk of
   circular imports (jobs imports printers, printers imports jobs for job state).

2. **Interface contracts (Dependency Inversion)** — Define an abstract base class (ABC) in
   `core/interfaces/` that specifies WHAT the callee must provide, without specifying HOW.
   The printers module implements `PrinterStateProvider`. The jobs module depends on
   `PrinterStateProvider`, not on `modules.printers`. At runtime, the app factory injects the
   concrete implementation.

## Decision

Define 5 ABC interfaces in `core/interfaces/`:

| Interface | Consumer | Provider |
|-----------|----------|----------|
| `PrinterStateProvider` | jobs (scheduler) | printers |
| `EventBus` | all modules | core |
| `NotificationDispatcher` | printers, jobs, vision | notifications |
| `OrgSettingsProvider` | notifications, printers | organizations |
| `JobStateProvider` | printers (to update state after job completes) | jobs |

Each module declares its `IMPLEMENTS` and `REQUIRES` lists in its `__init__.py` manifest.
The app factory (Phase 6) validates that all `REQUIRES` entries are satisfied before starting.

The ABCs live in `core/interfaces/` — a directory that modules can import from without creating
circular dependencies (core never imports from modules).

## Consequences

### Positive
- Module dependencies are explicit and machine-verifiable (the `REQUIRES` manifest list)
- The app factory can enforce that all required interfaces are implemented before starting
- Circular imports are impossible by design: modules import from `core/interfaces/`, never from
  each other
- Implementations can be swapped for testing (mock `PrinterStateProvider` that returns canned data)
- The interface catalog in `core/interfaces/` documents all synchronous cross-module contracts

### Negative
- More boilerplate: each synchronous cross-module call requires an interface definition,
  an implementation, and registration in the app factory
- Python's ABC enforcement is runtime, not compile-time — a missing `@abstractmethod`
  implementation raises at instantiation, not at import
- The app factory (Phase 6) must be built before interfaces are fully validated — until then,
  `IMPLEMENTS`/`REQUIRES` declarations are documentation-only

### Neutral
- Only 5 interfaces are needed for the 12 modules. Most cross-module communication is either
  fire-and-forget (event bus) or read-only (org settings lookup). The interface count is
  intentionally small.
- The `EventBus` is itself an interface — the concrete `InMemoryEventBus` in `core/event_bus.py`
  implements it. This allows the event bus implementation to be swapped (e.g. for a Redis-backed
  bus in a future distributed deployment) without changing any module code.
