"""Contract tests for ODIN_TELEMETRY_V2 feature flag (T4.4)."""
from __future__ import annotations

import pytest

from backend.modules.printers.telemetry.feature_flag import (
    is_shadow_enabled,
    is_v2_enabled,
    mode,
)


@pytest.fixture
def clear_env(monkeypatch):
    monkeypatch.delenv("ODIN_TELEMETRY_V2", raising=False)


class TestMode:
    def test_default_is_legacy(self, clear_env):
        assert mode() == "legacy"

    def test_zero_is_legacy(self, monkeypatch):
        monkeypatch.setenv("ODIN_TELEMETRY_V2", "0")
        assert mode() == "legacy"

    def test_one_is_v2(self, monkeypatch):
        monkeypatch.setenv("ODIN_TELEMETRY_V2", "1")
        assert mode() == "v2"

    def test_shadow(self, monkeypatch):
        monkeypatch.setenv("ODIN_TELEMETRY_V2", "shadow")
        assert mode() == "shadow"

    def test_whitespace_accepted(self, monkeypatch):
        monkeypatch.setenv("ODIN_TELEMETRY_V2", "  1  ")
        assert mode() == "v2"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("ODIN_TELEMETRY_V2", "SHADOW")
        assert mode() == "shadow"

    def test_invalid_fails_loud(self, monkeypatch):
        """Typo in env → raise. Don't default silently."""
        monkeypatch.setenv("ODIN_TELEMETRY_V2", "true")
        with pytest.raises(ValueError, match="ODIN_TELEMETRY_V2"):
            mode()


class TestConvenienceFunctions:
    def test_is_v2_enabled_default_false(self, clear_env):
        assert is_v2_enabled() is False

    def test_is_v2_enabled_true_when_1(self, monkeypatch):
        monkeypatch.setenv("ODIN_TELEMETRY_V2", "1")
        assert is_v2_enabled() is True

    def test_is_v2_enabled_false_in_shadow(self, monkeypatch):
        """Shadow runs both — but the 'primary' is still legacy."""
        monkeypatch.setenv("ODIN_TELEMETRY_V2", "shadow")
        assert is_v2_enabled() is False

    def test_is_shadow_enabled(self, monkeypatch):
        monkeypatch.setenv("ODIN_TELEMETRY_V2", "shadow")
        assert is_shadow_enabled() is True

    def test_is_shadow_enabled_false_when_v2(self, monkeypatch):
        monkeypatch.setenv("ODIN_TELEMETRY_V2", "1")
        assert is_shadow_enabled() is False
