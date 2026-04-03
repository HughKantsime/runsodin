# ADR-0003: Event Bus Over Direct Cross-Module Imports

**Status:** accepted
**Date:** 2026-02-26
**Deciders:** Shane Smith

## Context

O.D.I.N.'s `printer_events.py` (1049 lines) is the central coupling point in the backend. It
dispatches state changes from 4 printer monitor daemons to every downstream consumer: WebSocket
clients, alert notifications, MQTT republish, smart plugs, and HMS error decoding.

The current implementation uses direct imports wrapped in try/except:

```python
# printer_events.py (current)
try:
    from mqtt_republish import ...
except ImportError:
    pass

try:
    from ws_hub import push_event
except ImportError:
    pass

try:
    from smart_plug import control_plug
except ImportError:
    pass
```

This pattern indicates the author recognized the coupling was a problem (the try/except is a smell
of "I know this shouldn't be here") but worked around it rather than solving it.

Once modules are in separate directories (Phase 3), these direct imports become circular: the
printers module would import from notifications, which imports from printers for alert data. The
direct-import pattern cannot survive modularization.

Alternatives considered:
1. **Dependency injection (DI container)** — Pass notification/WebSocket services as constructor
   arguments. Solves coupling but requires refactoring every function signature in printer_events.
2. **Shared service layer** — Create a `services/` directory that all modules import from.
   Solves the circular import but still creates tight coupling.
3. **Event bus (pub/sub)** — Modules publish typed events; subscribers react without the
   publisher knowing who is listening.

## Decision

Implement an in-memory event bus in `core/event_bus.py` as a concrete implementation of the
`EventBus` ABC defined in `core/interfaces/event_bus.py`.

The printers module publishes typed events (see `core/events.py` for the catalog). Downstream
modules subscribe to those events during app startup. The publisher never imports the subscriber.

```
printers module → publishes "printer.state_changed"
                              ↓
notifications module ← subscribes → dispatches alert
ws_hub ← subscribes → broadcasts to WebSocket clients
smart_plug module ← subscribes → controls plugs
mqtt_republish module ← subscribes → forwards to external MQTT
```

The event bus starts synchronous (Phase 4). Async support can be added later if performance
requires it (the current event dispatch is already synchronous).

## Consequences

### Positive
- Removing a module requires only removing its directory and its event subscriptions — no other
  module's code changes
- New subscribers can be added without modifying the publisher (open/closed principle)
- The event catalog in `core/events.py` documents all cross-module communication in one place
- Testable: each module can be tested by publishing events directly to the bus and asserting
  subscriber behavior, without starting the full app
- The try/except import hacks in `printer_events.py` are eliminated

### Negative
- Harder to trace execution flow: "what happens when a print completes?" requires searching all
  subscribers of `job.completed`, rather than reading a single function
- Synchronous event bus creates a performance risk if a slow subscriber (e.g. email send) blocks
  the printer monitor loop — mitigated in Phase 4 by careful handler design (handlers should be
  fast; async dispatch is the Phase 4+ upgrade path)
- Event contracts must be maintained: adding a field to an event payload is safe; removing one
  is a breaking change that must be coordinated across all subscribers

### Neutral
- The `Event` dataclass in `core/interfaces/event_bus.py` is the contract between publisher and
  subscriber. It contains `event_type`, `source_module`, and `data` (dict). The dict is untyped
  by design — typed event payloads can be added in Phase 9 contract tests.
