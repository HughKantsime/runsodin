"""
Unit tests for backend/printer_models.py — normalize_model_name().

Pure logic tests, no live API required.

Run:
    pytest tests/test_printer_models.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from modules.printers.printer_models import normalize_model_name


class TestBambuModels:
    def test_known_codes(self):
        # Bambu Studio machine model ids (resources/profiles/BBL/machine).
        assert normalize_model_name("bambu", "BL-P001") == "X1C"
        assert normalize_model_name("bambu", "BL-P002") == "X1"
        assert normalize_model_name("bambu", "C13") == "X1E"
        assert normalize_model_name("bambu", "C11") == "P1P"
        assert normalize_model_name("bambu", "C12") == "P1S"
        assert normalize_model_name("bambu", "N2S") == "A1"
        assert normalize_model_name("bambu", "N1") == "A1 Mini"
        assert normalize_model_name("bambu", "O1D") == "H2D"
        assert normalize_model_name("bambu", "O1E") == "H2D Pro"
        assert normalize_model_name("bambu", "O1S") == "H2S"
        assert normalize_model_name("bambu", "O1C2") == "H2C"
        assert normalize_model_name("bambu", "N7") == "P2S"

    def test_new_model_ids_resolve_to_canonical_machines(self):
        from core.printer_identity import canonical_machine
        assert [canonical_machine(c) for c in ("O1E", "O1S", "O1C2", "N7", "Bambu Lab H2D Pro", "Bambu Lab P2S")] == [
            "h2d-pro", "h2s", "h2c", "p2s", "h2d-pro", "p2s"]
        assert normalize_model_name("bambu", "Bambu Lab H2D Pro") == "H2D Pro"

    def test_ota_product_names(self):
        assert normalize_model_name("bambu", "Bambu Lab A1") == "A1"
        assert normalize_model_name("bambu", "Bambu Lab H2D") == "H2D"
        assert normalize_model_name("bambu", "Bambu Lab X1 Carbon") == "X1C"
        assert normalize_model_name("bambu", "Bambu Lab A1 mini") == "A1 Mini"

    def test_unsourced_codes_are_not_guessed(self):
        for code in ("BL-P003", "BL-P00501", "BL-A001", "BL-H001"):
            assert normalize_model_name("bambu", code) == code

    def test_unknown_code_passthrough(self):
        assert normalize_model_name("bambu", "BL-X999") == "BL-X999"

    def test_whitespace_stripped(self):
        assert normalize_model_name("bambu", "  BL-P001  ") == "X1C"


class TestPrusaLinkModels:
    def test_known_models(self):
        assert normalize_model_name("prusalink", "MK4S") == "MK4S"
        assert normalize_model_name("prusalink", "MK4") == "MK4"
        assert normalize_model_name("prusalink", "MK39") == "MK3.9"
        assert normalize_model_name("prusalink", "MK3.9") == "MK3.9"
        assert normalize_model_name("prusalink", "MINI") == "MINI+"
        assert normalize_model_name("prusalink", "XL") == "XL"
        assert normalize_model_name("prusalink", "CORE_ONE") == "CORE One"

    def test_unknown_passthrough(self):
        assert normalize_model_name("prusalink", "MK5") == "MK5"


class TestElegooModels:
    def test_known_models(self):
        assert normalize_model_name("elegoo", "Centauri Carbon") == "Centauri Carbon"
        assert normalize_model_name("elegoo", "Neptune 4 Pro") == "Neptune 4 Pro"
        assert normalize_model_name("elegoo", "Saturn 4 Ultra") == "Saturn 4 Ultra"

    def test_case_insensitive(self):
        assert normalize_model_name("elegoo", "centauri carbon") == "Centauri Carbon"
        assert normalize_model_name("elegoo", "CENTAURI CARBON") == "Centauri Carbon"

    def test_unknown_passthrough(self):
        assert normalize_model_name("elegoo", "Mars 5") == "Mars 5"


class TestMoonraker:
    def test_always_returns_none(self):
        assert normalize_model_name("moonraker", "Voron") is None
        assert normalize_model_name("moonraker", "anything") is None


class TestEdgeCases:
    def test_none_input(self):
        assert normalize_model_name("bambu", None) is None

    def test_empty_string(self):
        assert normalize_model_name("bambu", "") is None

    def test_whitespace_only(self):
        assert normalize_model_name("bambu", "   ") is None

    def test_unknown_api_type(self):
        assert normalize_model_name("unknown", "SomeModel") == "SomeModel"

    def test_none_api_type(self):
        assert normalize_model_name(None, "SomeModel") == "SomeModel"

    def test_case_insensitive_api_type(self):
        assert normalize_model_name("BAMBU", "BL-P001") == "X1C"
        assert normalize_model_name("PrusaLink", "MK4") == "MK4"
