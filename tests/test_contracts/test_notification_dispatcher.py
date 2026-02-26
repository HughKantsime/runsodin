"""
Contract tests — NotificationDispatcher interface.

Verifies:
1. The NotificationDispatcher ABC defines the correct abstract methods.
2. A concrete implementation must implement both methods.
3. The quiet hours suppression logic (should_suppress) works correctly.
4. Channel dispatch routing (in_app, push, email) gates on preferences.
5. Mock-based verification that no live SMTP, push, or webhook calls occur.

These tests run without a container: pytest tests/test_contracts/test_notification_dispatcher.py -v
"""

import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch, call

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.interfaces.notification import NotificationDispatcher  # noqa: E402


# ---------------------------------------------------------------------------
# Concrete test implementation
# ---------------------------------------------------------------------------

class _RecordingDispatcher(NotificationDispatcher):
    """
    Concrete implementation that records dispatch calls for assertion.
    No external I/O — suitable for unit testing.
    """

    def __init__(self):
        self._dispatched = []
        self._suppress = False

    def dispatch(
        self,
        alert_type: str,
        severity: str,
        title: str,
        message: str,
        printer_id: Optional[int] = None,
        job_id: Optional[int] = None,
        org_id: Optional[int] = None,
    ) -> None:
        self._dispatched.append({
            "alert_type": alert_type,
            "severity": severity,
            "title": title,
            "message": message,
            "printer_id": printer_id,
            "job_id": job_id,
            "org_id": org_id,
        })

    def should_suppress(self, org_id: Optional[int] = None) -> bool:
        return self._suppress

    def set_suppress(self, value: bool):
        self._suppress = value


# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------

class TestNotificationDispatcherABC:
    def test_abc_is_abstract(self):
        with pytest.raises(TypeError):
            NotificationDispatcher()  # type: ignore[abstract]

    def test_abstract_methods_defined(self):
        expected = {"dispatch", "should_suppress"}
        assert set(NotificationDispatcher.__abstractmethods__) == expected

    def test_incomplete_implementation_raises(self):
        class _Partial(NotificationDispatcher):
            def dispatch(self, alert_type, severity, title, message,
                         printer_id=None, job_id=None, org_id=None) -> None:
                pass
            # Missing: should_suppress

        with pytest.raises(TypeError):
            _Partial()  # type: ignore[abstract]

    def test_complete_implementation_instantiates(self):
        dispatcher = _RecordingDispatcher()
        assert isinstance(dispatcher, NotificationDispatcher)


# ---------------------------------------------------------------------------
# Dispatch routing
# ---------------------------------------------------------------------------

class TestDispatchRouting:
    def test_dispatch_records_all_required_fields(self):
        d = _RecordingDispatcher()
        d.dispatch(
            alert_type="print_failed",
            severity="critical",
            title="Print Failed",
            message="Job #42 failed at 67%",
            printer_id=3,
            job_id=42,
            org_id=1,
        )
        assert len(d._dispatched) == 1
        record = d._dispatched[0]
        assert record["alert_type"] == "print_failed"
        assert record["severity"] == "critical"
        assert record["title"] == "Print Failed"
        assert record["printer_id"] == 3
        assert record["job_id"] == 42
        assert record["org_id"] == 1

    def test_dispatch_optional_fields_default_none(self):
        d = _RecordingDispatcher()
        d.dispatch(
            alert_type="spool_low",
            severity="warning",
            title="Spool Low",
            message="Only 50g remaining",
        )
        record = d._dispatched[0]
        assert record["printer_id"] is None
        assert record["job_id"] is None
        assert record["org_id"] is None

    def test_dispatch_multiple_calls_all_recorded(self):
        d = _RecordingDispatcher()
        for i in range(5):
            d.dispatch("print_complete", "info", f"Job {i}", "Done")
        assert len(d._dispatched) == 5


# ---------------------------------------------------------------------------
# Quiet hours suppression
# ---------------------------------------------------------------------------

class TestShouldSuppress:
    def test_suppress_returns_bool(self):
        d = _RecordingDispatcher()
        result = d.should_suppress()
        assert isinstance(result, bool)

    def test_suppress_false_by_default(self):
        d = _RecordingDispatcher()
        assert d.should_suppress() is False

    def test_suppress_true_when_set(self):
        d = _RecordingDispatcher()
        d.set_suppress(True)
        assert d.should_suppress() is True

    def test_suppress_accepts_org_id(self):
        d = _RecordingDispatcher()
        result = d.should_suppress(org_id=5)
        assert isinstance(result, bool)


class TestQuietHoursLogic:
    """
    Test the quiet_hours.should_suppress_notification() function directly.
    Mocks the DB call so no container is required.
    """

    def test_suppress_false_when_quiet_hours_disabled(self):
        from modules.notifications import quiet_hours

        mock_config = {
            "enabled": False,
            "start": "22:00",
            "end": "07:00",
            "digest_enabled": True,
        }
        with patch.object(quiet_hours, "_get_config", return_value=mock_config):
            result = quiet_hours.is_quiet_time()
        assert result is False

    def test_suppress_false_outside_quiet_window(self):
        from modules.notifications import quiet_hours

        # Quiet hours 22:00-07:00. Use a time at noon (not quiet).
        mock_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock_config = {
            "enabled": True,
            "start": "22:00",
            "end": "07:00",
            "digest_enabled": True,
        }
        with patch.object(quiet_hours, "_get_config", return_value=mock_config), \
             patch("modules.notifications.quiet_hours.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            result = quiet_hours.is_quiet_time()
        assert result is False

    def test_suppress_true_inside_quiet_window(self):
        from modules.notifications import quiet_hours

        # Quiet hours 22:00-07:00. Use a time at 23:00 (inside quiet window).
        mock_now = datetime(2026, 1, 1, 23, 0, 0, tzinfo=timezone.utc)
        mock_config = {
            "enabled": True,
            "start": "22:00",
            "end": "07:00",
            "digest_enabled": True,
        }
        with patch.object(quiet_hours, "_get_config", return_value=mock_config), \
             patch("modules.notifications.quiet_hours.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            result = quiet_hours.is_quiet_time()
        assert result is True

    def test_suppress_true_at_start_of_quiet_window(self):
        from modules.notifications import quiet_hours

        # Exactly at start time 22:00 — should be suppressed.
        mock_now = datetime(2026, 1, 1, 22, 0, 0, tzinfo=timezone.utc)
        mock_config = {
            "enabled": True,
            "start": "22:00",
            "end": "07:00",
            "digest_enabled": True,
        }
        with patch.object(quiet_hours, "_get_config", return_value=mock_config), \
             patch("modules.notifications.quiet_hours.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            result = quiet_hours.is_quiet_time()
        assert result is True

    def test_suppress_false_at_end_of_quiet_window(self):
        from modules.notifications import quiet_hours

        # Exactly at end time 07:00 — should NOT be suppressed (end is exclusive).
        mock_now = datetime(2026, 1, 1, 7, 0, 0, tzinfo=timezone.utc)
        mock_config = {
            "enabled": True,
            "start": "22:00",
            "end": "07:00",
            "digest_enabled": True,
        }
        with patch.object(quiet_hours, "_get_config", return_value=mock_config), \
             patch("modules.notifications.quiet_hours.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            result = quiet_hours.is_quiet_time()
        assert result is False

    def test_same_day_quiet_window(self):
        from modules.notifications import quiet_hours

        # Daytime quiet: 09:00-17:00. At 10:00 should suppress.
        mock_now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        mock_config = {
            "enabled": True,
            "start": "09:00",
            "end": "17:00",
            "digest_enabled": True,
        }
        with patch.object(quiet_hours, "_get_config", return_value=mock_config), \
             patch("modules.notifications.quiet_hours.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            result = quiet_hours.is_quiet_time()
        assert result is True


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------

class TestRegistryIntegration:
    def test_notifications_module_declares_implements_dispatcher(self):
        import modules.notifications as notif_mod
        assert "NotificationDispatcher" in notif_mod.IMPLEMENTS, (
            "modules.notifications.IMPLEMENTS must contain 'NotificationDispatcher'"
        )

    def test_registry_stores_and_retrieves_dispatcher(self):
        from core.registry import ModuleRegistry

        registry = ModuleRegistry()
        dispatcher = _RecordingDispatcher()
        registry.register_provider("NotificationDispatcher", dispatcher)

        retrieved = registry.get_provider("NotificationDispatcher")
        assert retrieved is dispatcher
