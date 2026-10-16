"""The single Bambu model table matches Bambu's own published model ids.

EXPECTED is written by hand from Bambu Studio 02.05.00.66's machine profiles
(/Applications/BambuStudio.app/Contents/Resources/profiles/BBL/machine/
"Bambu Lab <model>.json", type "machine_model", key "model_id"). When those
profiles are installed on the host they are read and checked as well.
"""
import json
from pathlib import Path

import pytest

EXPECTED = {
    'BL-P001': 'X1C',      # Bambu Lab X1 Carbon
    'BL-P002': 'X1',       # Bambu Lab X1
    'C13': 'X1E',          # Bambu Lab X1E
    'C11': 'P1P',          # Bambu Lab P1P
    'C12': 'P1S',          # Bambu Lab P1S
    'N7': 'P2S',           # Bambu Lab P2S
    'N1': 'A1 Mini',       # Bambu Lab A1 mini
    'N2S': 'A1',           # Bambu Lab A1 (also the owned A1 3MF fixture's printer_model_id)
    'O1D': 'H2D',          # Bambu Lab H2D
    'O1E': 'H2D Pro',      # Bambu Lab H2D Pro
    'O1S': 'H2S',          # Bambu Lab H2S
    'O1C2': 'H2C',         # Bambu Lab H2C
}
PROFILES = Path('/Applications/BambuStudio.app/Contents/Resources/profiles/BBL/machine')
FRIENDLY = {'X1 Carbon': 'X1C', 'A1 mini': 'A1 Mini'}


def test_model_table_is_exactly_the_bambu_studio_ids():
    from modules.printers.printer_models import BAMBU_MODEL_IDS
    assert BAMBU_MODEL_IDS == EXPECTED


@pytest.mark.parametrize('code,name', sorted(EXPECTED.items()))
def test_every_code_resolves_identically_in_all_consumers(code, name):
    from core.printer_identity import canonical_machine
    from modules.models_library.threemf_parser import _friendly_printer_name
    from modules.printers.printer_models import normalize_model_name
    assert normalize_model_name('bambu', code) == name
    assert _friendly_printer_name(code) == name
    assert canonical_machine(code) == canonical_machine(name) is not None


@pytest.mark.skipif(not PROFILES.is_dir(), reason='Bambu Studio profiles are not installed on this host')
def test_installed_bambu_studio_profiles_agree():
    found = {}
    for path in PROFILES.glob('Bambu Lab *.json'):
        data = json.loads(path.read_text())
        if data.get('type') == 'machine_model' and data.get('model_id'):
            model = data['name'].removeprefix('Bambu Lab ')
            found[data['model_id']] = FRIENDLY.get(model, model)
    assert found and {code: found[code] for code in EXPECTED if code in found} == {c: n for c, n in EXPECTED.items() if c in found}
    assert set(found) <= set(EXPECTED), f'Bambu Studio has ids the table lacks: {sorted(set(found) - set(EXPECTED))}'
