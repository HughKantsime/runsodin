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
        assert normalize_model_name("bambu", "BL-P001") == "X1C"
        assert normalize_model_name("bambu", "BL-P002") == "X1E"
        assert normalize_model_name("bambu", "BL-P003") == "X1"
        assert normalize_model_name("bambu", "C11") == "P1S"
        assert normalize_model_name("bambu", "BL-P00501") == "P1S"
        assert normalize_model_name("bambu", "C12") == "P1P"
        assert normalize_model_name("bambu", "BL-P00401") == "P1P"
        assert normalize_model_name("bambu", "N2S") == "A1"
        assert normalize_model_name("bambu", "BL-A001") == "A1"
        assert normalize_model_name("bambu", "N1") == "A1 Mini"
        assert normalize_model_name("bambu", "BL-A002") == "A1 Mini"
        assert normalize_model_name("bambu", "BL-H001") == "H2D"

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
