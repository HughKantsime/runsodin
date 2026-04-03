# core/event_bus.py — InMemoryEventBus implementation
#
# Concrete synchronous event bus. Single-process pub/sub for decoupling modules.
# Monitors and the FastAPI process share a SQLite DB (ws_events) for cross-process
# communication; this bus handles in-process cross-module decoupling only.

import logging
from collections import defaultdict
from typing import Callable, Any

from core.interfaces.event_bus import EventBus, Event

log = logging.getLogger("event_bus")


class InMemoryEventBus(EventBus):
    """
    Synchronous in-process event bus.

    Handlers are called in registration order. Exceptions in one handler do not
    prevent subsequent handlers from running. All calls are synchronous — no
    async complexity at this stage.
    """

    def __init__(self):
        # event_type -> list of callables
        self._handlers: dict[str, list[Callable[[Event], Any]]] = defaultdict(list)
        # wildcard handlers subscribed to "*" receive every event
        self._wildcard_handlers: list[Callable[[Event], Any]] = []

    def publish(self, event: Event) -> None:
        """Dispatch an event to all registered handlers for its type, then wildcards."""
        handlers = list(self._handlers.get(event.event_type, []))
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                log.error(
                    f"Event handler {handler!r} raised for event "
                    f"'{event.event_type}': {e}",
                    exc_info=True,
                )

        for handler in list(self._wildcard_handlers):
            try:
                handler(event)
            except Exception as e:
                log.error(
                    f"Wildcard handler {handler!r} raised for event "
                    f"'{event.event_type}': {e}",
                    exc_info=True,
                )

    def subscribe(self, event_type: str, handler: Callable[[Event], Any]) -> None:
        """
        Register a handler for an event type.

        Use event_type="*" to receive all events (wildcard).
        """
        if event_type == "*":
            if handler not in self._wildcard_handlers:
                self._wildcard_handlers.append(handler)
        else:
            if handler not in self._handlers[event_type]:
                self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        """Remove a previously registered handler."""
        if event_type == "*":
            try:
                self._wildcard_handlers.remove(handler)
            except ValueError:
                pass
        else:
            try:
                self._handlers[event_type].remove(handler)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_bus: InMemoryEventBus = InMemoryEventBus()


def get_event_bus() -> InMemoryEventBus:
    """Return the application-level event bus singleton."""
    return _bus
