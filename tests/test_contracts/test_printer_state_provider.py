"""
Contract tests — PrinterStateProvider interface.

Verifies:
1. The PrinterStateProvider ABC defines the correct abstract methods.
2. A concrete implementation must implement all four methods.
3. The documented return shapes are respected.

These tests run without a container: pytest tests/test_contracts/test_printer_state_provider.py -v
"""

import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.interfaces.printer_state import PrinterStateProvider  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ConcreteProvider(PrinterStateProvider):
    """Minimal concrete implementation used to verify the interface contract."""

    def get_printer_status(self, printer_id: int) -> dict:
        return {
            "state": "idle",
            "progress": 0,
            "temps": {"bed": 25.0, "nozzle": 22.0},
            "ams_state": None,
            "online": True,
        }

    def get_printer_info(self, printer_id: int) -> dict:
        return {
            "id": printer_id,
            "name": "Test Printer",
            "model": "X1C",
            "api_type": "bambu",
            "ip": "192.168.1.100",
            "org_id": 1,
            "bed_x": 256,
            "bed_y": 256,
        }

    def get_available_printers(self, org_id: Optional[int] = None) -> list:
        return [{"id": 1, "name": "Test Printer", "org_id": org_id or 1}]

    def get_printer_slots(self, printer_id: int) -> list:
        return [{"slot": 0, "color": "#FF0000", "filament_type": "PLA", "material": "PLA"}]


# ---------------------------------------------------------------------------
# Interface contract
# ---------------------------------------------------------------------------

class TestPrinterStateProviderABC:
    """The ABC defines exactly the methods we depend on."""

    def test_provider_abc_is_abstract(self):
        """Instantiating the ABC directly must raise TypeError."""
        with pytest.raises(TypeError):
            PrinterStateProvider()  # type: ignore[abstract]

    def test_all_abstract_methods_defined(self):
        expected = {"get_printer_status", "get_printer_info", "get_available_printers", "get_printer_slots"}
        actual = set(PrinterStateProvider.__abstractmethods__)
        assert actual == expected, (
            f"PrinterStateProvider abstract methods changed.\n"
            f"  Expected: {sorted(expected)}\n"
            f"  Got:      {sorted(actual)}"
        )

    def test_incomplete_implementation_raises(self):
        """A class that does not implement all methods cannot be instantiated."""
        class _Partial(PrinterStateProvider):
            def get_printer_status(self, printer_id: int) -> dict:
                return {}
            # Missing: get_printer_info, get_available_printers, get_printer_slots

        with pytest.raises(TypeError):
            _Partial()  # type: ignore[abstract]

    def test_complete_implementation_instantiates(self):
        provider = _ConcreteProvider()
        assert isinstance(provider, PrinterStateProvider)


# ---------------------------------------------------------------------------
# Return shape contracts
# ---------------------------------------------------------------------------

class TestPrinterStatusShape:
    """get_printer_status() must return the documented keys."""

    REQUIRED_KEYS = {"state", "progress", "temps", "ams_state", "online"}

    def test_status_has_required_keys(self):
        provider = _ConcreteProvider()
        status = provider.get_printer_status(printer_id=1)
        missing = self.REQUIRED_KEYS - set(status.keys())
        assert not missing, f"get_printer_status() missing keys: {missing}"

    def test_state_is_string(self):
        status = _ConcreteProvider().get_printer_status(printer_id=1)
        assert isinstance(status["state"], str)

    def test_progress_is_numeric(self):
        status = _ConcreteProvider().get_printer_status(printer_id=1)
        assert isinstance(status["progress"], (int, float))

    def test_temps_is_dict(self):
        status = _ConcreteProvider().get_printer_status(printer_id=1)
        assert isinstance(status["temps"], dict)

    def test_online_is_bool(self):
        status = _ConcreteProvider().get_printer_status(printer_id=1)
        assert isinstance(status["online"], bool)


class TestPrinterInfoShape:
    """get_printer_info() must return the documented keys."""

    REQUIRED_KEYS = {"id", "name", "model", "api_type", "ip", "org_id", "bed_x", "bed_y"}

    def test_info_has_required_keys(self):
        provider = _ConcreteProvider()
        info = provider.get_printer_info(printer_id=1)
        missing = self.REQUIRED_KEYS - set(info.keys())
        assert not missing, f"get_printer_info() missing keys: {missing}"

    def test_id_matches_argument(self):
        provider = _ConcreteProvider()
        assert provider.get_printer_info(printer_id=42)["id"] == 42


class TestAvailablePrintersShape:
    """get_available_printers() must return a list of dicts."""

    def test_returns_list(self):
        provider = _ConcreteProvider()
        result = provider.get_available_printers()
        assert isinstance(result, list)

    def test_accepts_optional_org_id(self):
        provider = _ConcreteProvider()
        result = provider.get_available_printers(org_id=5)
        assert isinstance(result, list)

    def test_items_are_dicts(self):
        provider = _ConcreteProvider()
        for item in provider.get_available_printers():
            assert isinstance(item, dict)


class TestPrinterSlotsShape:
    """get_printer_slots() must return a list of dicts."""

    def test_returns_list(self):
        provider = _ConcreteProvider()
        result = provider.get_printer_slots(printer_id=1)
        assert isinstance(result, list)

    def test_items_are_dicts(self):
        provider = _ConcreteProvider()
        for slot in provider.get_printer_slots(printer_id=1):
            assert isinstance(slot, dict)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------

class TestRegistryIntegration:
    """The printers module registers itself as PrinterStateProvider."""

    def test_printers_module_declares_implements_provider(self):
        import modules.printers as printers_mod
        assert "PrinterStateProvider" in printers_mod.IMPLEMENTS, (
            "modules.printers.IMPLEMENTS must contain 'PrinterStateProvider'"
        )

    def test_registry_stores_and_retrieves_provider(self):
        from core.registry import ModuleRegistry

        registry = ModuleRegistry()
        provider = _ConcreteProvider()
        registry.register_provider("PrinterStateProvider", provider)

        retrieved = registry.get_provider("PrinterStateProvider")
        assert retrieved is provider

    def test_registry_returns_none_for_missing_provider(self):
        from core.registry import ModuleRegistry

        registry = ModuleRegistry()
        result = registry.get_provider("NonExistentInterface")
        assert result is None
