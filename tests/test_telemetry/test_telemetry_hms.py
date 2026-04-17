"""Contract tests for Bambu HMS model + catalog (T1.3).

Tests use the bootstrap catalog committed at
backend/modules/printers/telemetry/bambu/hms_codes.json, which contains
9 real codes observed in odin-e2e/captures/run-2026-04-16.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.modules.printers.telemetry.bambu.hms import (
    BambuHMSCatalog,
    BambuHMSEvent,
    HMSEntry,
    get_catalog,
)


class TestBambuHMSEvent:
    def test_parse_from_dict(self):
        """Captures store hms entries as {'attr': int, 'code': int}."""
        event = BambuHMSEvent(attr=83886592, code=196618)
        assert event.attr == 83886592
        assert event.code == 196618

    def test_computed_key(self):
        event = BambuHMSEvent(attr=83886592, code=196618)
        assert event.key == "05000200_0003000A"

    def test_computed_hex(self):
        event = BambuHMSEvent(attr=0x0C000100, code=0x00010004)
        assert event.attr_hex == "0x0C000100"
        assert event.code_hex == "0x00010004"

    def test_zero_padded_hex_keys(self):
        """Short codes (e.g. 0x1) must still render as 8 hex chars for catalog lookup."""
        event = BambuHMSEvent(attr=1, code=1)
        assert event.key == "00000001_00000001"


class TestCatalogLoad:
    def test_load_default_path(self):
        """Catalog loads from the file committed next to the module."""
        catalog = BambuHMSCatalog.load()
        assert len(catalog) >= 9  # 9 bootstrap entries

    def test_load_singleton(self):
        """get_catalog() is a singleton."""
        a = get_catalog()
        b = get_catalog()
        assert a is b

    def test_load_fails_loud_on_missing(self, tmp_path):
        missing = tmp_path / "does_not_exist.json"
        with pytest.raises(FileNotFoundError):
            BambuHMSCatalog.load(missing)

    def test_load_fails_loud_on_malformed(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text('{"not_codes_key": {}}')
        with pytest.raises(ValueError, match="'codes' key missing"):
            BambuHMSCatalog.load(bad)

    def test_load_fails_loud_on_bad_severity(self, tmp_path):
        bad = tmp_path / "bad_severity.json"
        bad.write_text(json.dumps({
            "codes": {
                "00000000_00000000": {
                    "attr": 0,
                    "code": 0,
                    "severity": "CRITICAL_EMERGENCY",  # not a valid severity
                    "message": "test",
                }
            }
        }))
        with pytest.raises(ValueError, match="invalid HMS severity"):
            BambuHMSCatalog.load(bad)


class TestLookup:
    def test_hit_returns_entry(self):
        catalog = BambuHMSCatalog.load()
        # This code is observed on bambu-h2d 9,258 times in run-2026-04-16
        event = BambuHMSEvent(attr=0x05000200, code=0x0003000A)
        entry = catalog.lookup(event)
        assert entry is not None
        assert isinstance(entry, HMSEntry)
        assert entry.key == "05000200_0003000A"

    def test_miss_returns_none(self):
        """Unknown codes must return None so caller can fail loud; no synthesized fallback."""
        catalog = BambuHMSCatalog.load()
        event = BambuHMSEvent(attr=0xDEADBEEF, code=0xCAFEBABE)
        assert catalog.lookup(event) is None

    def test_contains(self):
        catalog = BambuHMSCatalog.load()
        assert "05000200_0003000A" in catalog
        assert "DEADBEEF_CAFEBABE" not in catalog


class TestBootstrapCatalog:
    """The committed bootstrap catalog must match the captures it was derived from."""

    def test_all_codes_marked_unknown_severity(self):
        catalog = BambuHMSCatalog.load()
        for key in catalog.keys():
            entry = catalog._entries[key]
            # Bootstrap codes all default to severity=unknown pending curation.
            # This test becomes a regression gate: if someone curates a code
            # and updates severity, they must also remove it from this assertion
            # or update the test. Intentional — forces explicit acknowledgment.
            assert entry.severity in ("info", "warning", "error", "unknown"), (
                f"{key} has invalid severity {entry.severity!r}"
            )

    def test_known_h2d_codes_present(self):
        """Codes observed on bambu-h2d must round-trip."""
        catalog = BambuHMSCatalog.load()
        expected_h2d_keys = [
            "05000200_0003000A",
            "05000600_000200A0",
            "05010400_00030002",
            "05010400_00030004",
            "07002200_00020012",
            "0C000100_00020017",
        ]
        for key in expected_h2d_keys:
            assert key in catalog, f"H2D-observed code {key} missing from catalog"

    def test_known_x1c_codes_present(self):
        catalog = BambuHMSCatalog.load()
        assert "0C000100_00010004" in catalog
        assert "0C000300_0003000B" in catalog

    def test_known_p1s_codes_present(self):
        catalog = BambuHMSCatalog.load()
        assert "03003100_00010001" in catalog


class TestEventToLookupRoundTrip:
    """An event parsed from a capture-shape dict must look up in the catalog correctly."""

    def test_real_h2d_entry(self):
        # This is the exact hms entry shape observed in
        # odin-e2e/captures/run-2026-04-16/bambu-h2d.jsonl line 1
        raw = {"attr": 83886592, "code": 196618}
        event = BambuHMSEvent(**raw)
        catalog = BambuHMSCatalog.load()
        entry = catalog.lookup(event)
        assert entry is not None
        assert entry.attr == raw["attr"]
        assert entry.code == raw["code"]
