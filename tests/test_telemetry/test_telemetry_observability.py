"""Contract tests for UnmappedFieldObserver (T1.9)."""
from __future__ import annotations

import pytest

from backend.modules.printers.telemetry.observability import (
    UnmappedFieldObserver,
    UnmappedFieldReport,
    _walk_leaves,
)


@pytest.fixture
def obs():
    return UnmappedFieldObserver()


class TestWalkLeaves:
    def test_flat_dict(self):
        leaves = list(_walk_leaves({"a": 1, "b": "x"}))
        assert ("a", 1) in leaves
        assert ("b", "x") in leaves
        assert len(leaves) == 2

    def test_nested_dict(self):
        leaves = dict(_walk_leaves({"outer": {"inner": 42}}))
        assert leaves == {"outer.inner": 42}

    def test_nested_list_of_dicts(self):
        leaves = dict(_walk_leaves({"items": [{"id": 1}, {"id": 2}]}))
        assert "items[].id" in leaves

    def test_list_of_scalars_skipped(self):
        """Lists of scalars don't generate leaves — only list-of-dict recurses."""
        leaves = list(_walk_leaves({"tags": ["a", "b", "c"]}))
        # No leaves because the list contains scalars, not dicts.
        # This is by design — we model container lists at the field level.
        assert leaves == []


class TestObserveBasics:
    def test_none_is_noop(self, obs):
        obs.observe("bambu", None)
        assert len(obs) == 0

    def test_empty_dict_is_noop(self, obs):
        obs.observe("bambu", {})
        assert len(obs) == 0

    def test_single_field_recorded(self, obs):
        obs.observe("bambu", {"new_field": 42})
        assert len(obs) == 1
        snap = obs.snapshot()
        assert snap[0].vendor == "bambu"
        assert snap[0].field_path == "new_field"
        assert snap[0].sample_type == "int"
        assert snap[0].sample_value == "42"
        assert snap[0].count == 1

    def test_multiple_fields_recorded(self, obs):
        obs.observe("bambu", {"a": 1, "b": "x", "c": 3.14})
        assert len(obs) == 3


class TestDedup:
    def test_repeated_field_increments_count(self, obs):
        obs.observe("bambu", {"new_field": 1})
        obs.observe("bambu", {"new_field": 2})
        obs.observe("bambu", {"new_field": 3})
        assert len(obs) == 1  # still one unique key
        snap = obs.snapshot()
        assert snap[0].count == 3
        # sample_value should reflect FIRST seen; empirical decision —
        # first-seen is more useful for debugging drift than last-seen
        assert snap[0].sample_value == "1"

    def test_per_vendor_keyed(self, obs):
        """Same field name on different vendors counts as different."""
        obs.observe("bambu", {"temp": 1})
        obs.observe("moonraker", {"temp": 2})
        assert len(obs) == 2


class TestLoggingDedup:
    def test_logs_once_per_key(self, obs, caplog):
        """Every (vendor, field) pair logs at WARN exactly once."""
        import logging
        caplog.set_level(logging.WARNING, logger="backend.modules.printers.telemetry.observability")

        obs.observe("bambu", {"first": 1})
        obs.observe("bambu", {"first": 2})  # same field — no new log
        obs.observe("bambu", {"second": 1})  # new field — new log
        obs.observe("bambu", {"second": 2})  # no new log

        warn_logs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warn_logs) == 2


class TestOverflow:
    def test_overflow_caps_memory(self, obs):
        """When MAX_UNIQUE_FIELDS hit, further new fields are dropped."""
        obs.MAX_UNIQUE_FIELDS = 3  # local override
        obs.observe("bambu", {"a": 1, "b": 2, "c": 3})
        assert len(obs) == 3
        obs.observe("bambu", {"d": 4})  # overflow
        assert len(obs) == 3  # still 3, `d` dropped
        # existing fields still tick
        obs.observe("bambu", {"a": 99})
        snap = obs.snapshot()
        a_entry = next(r for r in snap if r.field_path == "a")
        assert a_entry.count == 2

    def test_overflow_log_fires_once(self, obs, caplog):
        import logging
        caplog.set_level(logging.ERROR, logger="backend.modules.printers.telemetry.observability")
        obs.MAX_UNIQUE_FIELDS = 1
        obs.observe("bambu", {"first": 1})
        obs.observe("bambu", {"second": 2})
        obs.observe("bambu", {"third": 3})
        error_logs = [r for r in caplog.records if r.levelno == logging.ERROR]
        # One error at overflow, no subsequent repeats
        assert len(error_logs) == 1
        assert "capacity exceeded" in error_logs[0].message


class TestValueTruncation:
    def test_long_value_truncated(self, obs):
        obs.observe("bambu", {"big": "x" * 500})
        snap = obs.snapshot()
        assert len(snap[0].sample_value) == obs.VALUE_TRUNCATE  # 200


class TestNestedExtras:
    def test_nested_paths_surfaced(self, obs):
        obs.observe("bambu", {"print": {"new_nested_field": 42}})
        snap = obs.snapshot()
        assert any(r.field_path == "print.new_nested_field" for r in snap)

    def test_list_of_dict_paths(self, obs):
        obs.observe("bambu", {"modules": [{"new_field": 1}]})
        snap = obs.snapshot()
        assert any(r.field_path == "modules[].new_field" for r in snap)


class TestSnapshotImmutability:
    def test_snapshot_returns_frozen_reports(self, obs):
        obs.observe("bambu", {"a": 1})
        snap = obs.snapshot()
        assert isinstance(snap[0], UnmappedFieldReport)
        # UnmappedFieldReport is frozen
        with pytest.raises(Exception):
            snap[0].count = 999  # type: ignore[misc]

    def test_snapshot_is_sorted(self, obs):
        obs.observe("moonraker", {"z": 1})
        obs.observe("bambu", {"a": 1})
        obs.observe("bambu", {"b": 1})
        snap = obs.snapshot()
        # sorted by (vendor, field_path)
        assert [r.field_path for r in snap] == ["a", "b", "z"]


class TestReset:
    def test_reset_clears(self, obs):
        obs.observe("bambu", {"a": 1})
        assert len(obs) == 1
        obs.reset()
        assert len(obs) == 0


class TestIntegrationWithPydantic:
    """Live integration: parse real extra fields via _StrictBase and observe them."""

    def test_model_extra_flows_to_observer(self, obs):
        from backend.modules.printers.telemetry.bambu.raw import BambuPrintSection
        payload = {
            "gcode_state": "RUNNING",
            "mc_percent": 50,
            "new_firmware_field_xyz": "value",
            "another_unknown": 42,
        }
        p = BambuPrintSection.model_validate(payload)
        # model_extra contains ONLY the unknown fields
        obs.observe("bambu", p.model_extra)
        snap = obs.snapshot()
        paths = {r.field_path for r in snap}
        assert "new_firmware_field_xyz" in paths
        assert "another_unknown" in paths
        # known fields are NOT observed
        assert "gcode_state" not in paths
        assert "mc_percent" not in paths
