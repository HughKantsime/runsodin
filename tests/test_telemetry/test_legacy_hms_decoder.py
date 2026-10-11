"""Fixed expected translations and fallback behavior, independent of catalog counts."""
import pytest
from modules.printers.hms_codes import lookup_hms_code, lookup_print_error

@pytest.mark.parametrize('code,expected', [
    ('05000200_0003000A', 'AMS Hub: cutter failure.'),
    (' 05000200_0003000a ', 'AMS Hub: cutter failure.'),
    ('05000600_000200A0', 'AMS Hub: filament mapping error.'),
    ('0C000100_00010004', 'Extruder: temperature abnormal.'),
    ('03003100_00010001', 'AMS: error 0x31 (slot 1).'),
    ('DEADBEEF_CAFEBABE', 'Device 0xDE (unit 173): error 0xBE.'),
    ('0C00_C004', 'Possible spaghetti failure detected.'),
    (' 0c00-c004 ', 'Possible spaghetti failure detected.'),
    ('invalid', 'Unknown HMS error: INVALID'),
    ('GGGGGGGG_GGGGGGGG', 'HMS error: GGGGGGGG_GGGGGGGG'),
])
def test_hms_translation_and_safe_unknown_fallback(code, expected):
    assert lookup_hms_code(code) == expected


def test_unknown_print_action_is_not_reported_as_success():
    assert lookup_print_error(' dead_beef ') == 'Unknown print error: DEAD_BEEF'
