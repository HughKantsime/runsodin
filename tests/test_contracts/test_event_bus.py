"""
Contract tests — InMemoryEventBus.

Verifies publish/subscribe/unsubscribe behaviour of InMemoryEventBus including:
- Subscribers receive events they subscribed to.
- Unsubscribed handlers are not called.
- Handlers for different event types are not cross-triggered.
- Wildcard ("*") subscribers receive all events.
- Exceptions in one handler do not block other handlers.
- The singleton get_event_bus() is stable.

These tests run without a container: pytest tests/test_contracts/test_event_bus.py -v
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.interfaces.event_bus import Event, EventBus  # noqa: E402
from core.event_bus import InMemoryEventBus, get_event_bus  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(event_type: str, source: str = "test", data: dict = None) -> Event:
    return Event(event_type=event_type, source_module=source, data=data or {})


def _fresh_bus() -> InMemoryEventBus:
    """Return a new, isolated bus for each test."""
    return InMemoryEventBus()


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------

class TestEventBusABC:
    def test_event_bus_is_abstract(self):
        with pytest.raises(TypeError):
            EventBus()  # type: ignore[abstract]

    def test_in_memory_bus_is_concrete(self):
        bus = _fresh_bus()
        assert isinstance(bus, EventBus)

    def test_abstract_methods_defined(self):
        expected = {"publish", "subscribe", "unsubscribe"}
        assert set(EventBus.__abstractmethods__) == expected


# ---------------------------------------------------------------------------
# Publish / Subscribe
# ---------------------------------------------------------------------------

class TestPublishSubscribe:
    def test_subscriber_receives_event(self):
        bus = _fresh_bus()
        received = []
        bus.subscribe("job.completed", received.append)
        evt = _make_event("job.completed")
        bus.publish(evt)
        assert len(received) == 1
        assert received[0] is evt

    def test_subscriber_does_not_receive_other_event_types(self):
        bus = _fresh_bus()
        received = []
        bus.subscribe("job.completed", received.append)
        bus.publish(_make_event("job.started"))
        assert len(received) == 0

    def test_multiple_subscribers_all_receive_event(self):
        bus = _fresh_bus()
        calls_a, calls_b = [], []
        bus.subscribe("printer.state_changed", calls_a.append)
        bus.subscribe("printer.state_changed", calls_b.append)
        bus.publish(_make_event("printer.state_changed"))
        assert len(calls_a) == 1
        assert len(calls_b) == 1

    def test_same_handler_not_registered_twice(self):
        bus = _fresh_bus()
        calls = []
        handler = calls.append
        bus.subscribe("job.completed", handler)
        bus.subscribe("job.completed", handler)  # duplicate — should be ignored
        bus.publish(_make_event("job.completed"))
        assert len(calls) == 1

    def test_event_data_is_passed_through(self):
        bus = _fresh_bus()
        received = []
        bus.subscribe("job.started", received.append)
        data = {"job_id": 42, "printer_id": 7}
        bus.publish(_make_event("job.started", data=data))
        assert received[0].data == data

    def test_event_source_module_preserved(self):
        bus = _fresh_bus()
        received = []
        bus.subscribe("vision.detection", received.append)
        bus.publish(_make_event("vision.detection", source="vision"))
        assert received[0].source_module == "vision"


# ---------------------------------------------------------------------------
# Unsubscribe
# ---------------------------------------------------------------------------

class TestUnsubscribe:
    def test_unsubscribed_handler_not_called(self):
        bus = _fresh_bus()
        calls = []
        bus.subscribe("job.completed", calls.append)
        bus.unsubscribe("job.completed", calls.append)
        bus.publish(_make_event("job.completed"))
        assert len(calls) == 0

    def test_unsubscribe_only_removes_specified_handler(self):
        bus = _fresh_bus()
        calls_a, calls_b = [], []
        bus.subscribe("job.completed", calls_a.append)
        bus.subscribe("job.completed", calls_b.append)
        bus.unsubscribe("job.completed", calls_a.append)
        bus.publish(_make_event("job.completed"))
        assert len(calls_a) == 0
        assert len(calls_b) == 1

    def test_unsubscribe_nonexistent_handler_does_not_raise(self):
        bus = _fresh_bus()
        bus.unsubscribe("job.completed", lambda e: None)  # not registered — should not raise

    def test_unsubscribe_from_wrong_event_type_is_noop(self):
        bus = _fresh_bus()
        calls = []
        bus.subscribe("job.completed", calls.append)
        bus.unsubscribe("job.started", calls.append)  # wrong event type
        bus.publish(_make_event("job.completed"))
        assert len(calls) == 1  # still subscribed


# ---------------------------------------------------------------------------
# Wildcard subscription
# ---------------------------------------------------------------------------

class TestWildcardSubscription:
    def test_wildcard_receives_all_events(self):
        bus = _fresh_bus()
        received = []
        bus.subscribe("*", received.append)
        bus.publish(_make_event("job.completed"))
        bus.publish(_make_event("printer.error"))
        bus.publish(_make_event("vision.detection"))
        assert len(received) == 3

    def test_wildcard_and_specific_handler_both_receive(self):
        bus = _fresh_bus()
        specific, wildcard = [], []
        bus.subscribe("job.completed", specific.append)
        bus.subscribe("*", wildcard.append)
        bus.publish(_make_event("job.completed"))
        assert len(specific) == 1
        assert len(wildcard) == 1

    def test_wildcard_not_registered_twice(self):
        bus = _fresh_bus()
        calls = []
        handler = calls.append
        bus.subscribe("*", handler)
        bus.subscribe("*", handler)  # duplicate
        bus.publish(_make_event("job.completed"))
        assert len(calls) == 1

    def test_unsubscribe_wildcard(self):
        bus = _fresh_bus()
        calls = []
        bus.subscribe("*", calls.append)
        bus.unsubscribe("*", calls.append)
        bus.publish(_make_event("job.completed"))
        assert len(calls) == 0


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------

class TestErrorIsolation:
    def test_exception_in_handler_does_not_prevent_subsequent_handlers(self):
        bus = _fresh_bus()
        second_called = []

        def bad_handler(event):
            raise RuntimeError("handler explodes")

        bus.subscribe("job.completed", bad_handler)
        bus.subscribe("job.completed", second_called.append)

        bus.publish(_make_event("job.completed"))
        assert len(second_called) == 1, "Second handler must still be called after first raised"

    def test_exception_in_wildcard_handler_does_not_prevent_other_wildcards(self):
        bus = _fresh_bus()
        second_called = []

        def bad_wildcard(event):
            raise ValueError("wildcard explodes")

        bus.subscribe("*", bad_wildcard)
        bus.subscribe("*", second_called.append)

        bus.publish(_make_event("job.completed"))
        assert len(second_called) == 1


# ---------------------------------------------------------------------------
# Multiple publishes
# ---------------------------------------------------------------------------

class TestMultiplePublishes:
    def test_handler_called_once_per_publish(self):
        bus = _fresh_bus()
        calls = []
        bus.subscribe("job.completed", calls.append)
        for _ in range(5):
            bus.publish(_make_event("job.completed"))
        assert len(calls) == 5

    def test_no_side_effects_across_event_types(self):
        bus = _fresh_bus()
        completed, failed = [], []
        bus.subscribe("job.completed", completed.append)
        bus.subscribe("job.failed", failed.append)
        bus.publish(_make_event("job.completed"))
        bus.publish(_make_event("job.failed"))
        assert len(completed) == 1
        assert len(failed) == 1


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_get_event_bus_returns_same_instance(self):
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        assert bus1 is bus2, "get_event_bus() must return the same singleton instance"

    def test_singleton_is_in_memory_bus(self):
        bus = get_event_bus()
        assert isinstance(bus, InMemoryEventBus)


# ---------------------------------------------------------------------------
# Event wiring verification — published events have subscribers
# ---------------------------------------------------------------------------

from core import events as ev  # noqa: E402


# All event constants defined in core/events.py
ALL_EVENT_CONSTANTS = {
    name: getattr(ev, name)
    for name in dir(ev)
    if not name.startswith("_") and isinstance(getattr(ev, name), str)
}

# Events published by the codebase (source -> event_type):
#   notifications/job_events.py: JOB_STARTED, JOB_COMPLETED, JOB_FAILED, JOB_CANCELLED
#   notifications/alert_dispatch.py: "notifications.alert_dispatched" (not in events.py — ad-hoc)
PUBLISHED_EVENTS = {
    ev.JOB_STARTED,
    ev.JOB_COMPLETED,
    ev.JOB_FAILED,
    ev.JOB_CANCELLED,
    "notifications.alert_dispatched",
}

# Events subscribed to (from all register_subscribers calls):
#   ws_hub: JOB_STARTED, JOB_COMPLETED, JOB_FAILED, "notifications.alert_dispatched", "*"
#   smart_plug: JOB_STARTED, JOB_COMPLETED
#   mqtt_republish: PRINTER_STATE_CHANGED, PRINTER_CONNECTED, PRINTER_DISCONNECTED,
#                   JOB_STARTED, JOB_COMPLETED, JOB_FAILED, "notifications.alert_dispatched"
#   archive: JOB_COMPLETED, JOB_FAILED, JOB_CANCELLED
SUBSCRIBED_EVENTS = {
    ev.JOB_STARTED,
    ev.JOB_COMPLETED,
    ev.JOB_FAILED,
    ev.JOB_CANCELLED,
    ev.PRINTER_STATE_CHANGED,
    ev.PRINTER_CONNECTED,
    ev.PRINTER_DISCONNECTED,
    "notifications.alert_dispatched",
}


class TestEventWiring:
    """Verify that published events have subscribers and vice versa."""

    def test_all_published_events_have_subscribers(self):
        """Every event published by the codebase must have at least one subscriber."""
        # The wildcard subscriber in ws_hub catches everything, but we also
        # check for explicit subscribers.
        unsubscribed = PUBLISHED_EVENTS - SUBSCRIBED_EVENTS
        # Wildcard ("*") catches everything, so technically all are subscribed.
        # But we verify explicit subscription for non-wildcard events.
        assert not unsubscribed, (
            f"Published events with no explicit subscriber: {sorted(unsubscribed)}"
        )

    def test_all_subscribed_events_are_published_or_have_constant(self):
        """Every subscribed event should either be published or be a known constant."""
        # Some events (PRINTER_STATE_CHANGED etc.) are published by monitor daemons
        # via ws_hub.push_event, not via event_bus.publish. The mqtt_republish
        # subscriber receives them via the event bus only when published by
        # the FastAPI process. Monitor processes run in separate OS processes.
        known_events = set(ALL_EVENT_CONSTANTS.values()) | {"notifications.alert_dispatched"}
        unknown = SUBSCRIBED_EVENTS - known_events
        assert not unknown, (
            f"Subscribed events not defined in events.py: {sorted(unknown)}"
        )

    def test_all_event_constants_are_used(self):
        """Every constant in events.py should be either published or subscribed to somewhere."""
        all_used = PUBLISHED_EVENTS | SUBSCRIBED_EVENTS
        all_constant_values = set(ALL_EVENT_CONSTANTS.values())

        # Events that are defined but only used by monitor daemons (separate processes)
        # that publish via ws_hub.push_event rather than event_bus.publish.
        # These are still valid — they trigger alerts/logging in the monitor code directly.
        MONITOR_ONLY_EVENTS = {
            ev.PRINTER_ERROR,
            ev.PRINTER_HMS_CODE,
            ev.DETECTION_TRIGGERED,
            ev.DETECTION_AUTO_PAUSE,
            ev.SPOOL_LOW,
            ev.SPOOL_EMPTY,
            ev.CONSUMABLE_LOW,
            ev.BACKUP_COMPLETED,
            ev.LICENSE_CHANGED,
            ev.JOB_CREATED,
        }

        unused = all_constant_values - all_used - MONITOR_ONLY_EVENTS
        assert not unused, (
            f"Event constants defined but never published or subscribed: {sorted(unused)}. "
            f"If these are used by monitor daemons, add to MONITOR_ONLY_EVENTS."
        )

    def test_wiring_simulation(self):
        """Simulate the actual wiring from app.py lifespan to verify no import errors."""
        bus = _fresh_bus()

        # Import and call each register_subscribers function
        from core.ws_hub import subscribe_to_bus as ws_subscribe
        from modules.printers import register_subscribers as printers_register
        from modules.notifications import register_subscribers as notifications_register
        from modules.archives import register_subscribers as archives_register

        # These should not raise
        ws_subscribe(bus)
        printers_register(bus)
        notifications_register(bus)
        archives_register(bus)

        # Verify bus has handlers registered
        assert len(bus._handlers) > 0, "No event handlers registered after wiring"
        assert len(bus._wildcard_handlers) > 0, "No wildcard handlers registered"
