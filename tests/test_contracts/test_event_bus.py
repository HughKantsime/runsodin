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
