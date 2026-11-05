"""No model context means plain codes, never the old structural guesses."""
import pytest
from modules.printers.hms_codes import lookup_hms_code, lookup_print_error

@pytest.mark.parametrize('code,expected', [
    ('05000200_0003000A', '0500-0200-0003-000A'),
    (' 05000200_0003000a ', '0500-0200-0003-000A'),
    ('05000600_000200A0', '0500-0600-0002-00A0'),
    ('0C000100_00010004', '0C00-0100-0001-0004'),
    ('03003100_00010001', '0300-3100-0001-0001'),
    ('DEADBEEF_CAFEBABE', 'DEAD-BEEF-CAFE-BABE'),
    ('0C00_C004', '0C00-C004'),
    (' 0c00-c004 ', '0C00-C004'),
])
def test_hms_translation_and_safe_unknown_fallback(code, expected):
    assert lookup_hms_code(code) == 'Printer code ' + expected


def test_unknown_print_action_is_not_reported_as_success():
    assert lookup_print_error(' dead_beef ') == 'Printer code DEAD-BEEF'
