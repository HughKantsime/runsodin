"""The single Bambu model table matches Bambu's own published model ids.

EXPECTED is written by hand from Bambu Studio 02.05.00.66's machine profiles
(/Applications/BambuStudio.app/Contents/Resources/profiles/BBL/machine/
"Bambu Lab <model>.json", type "machine_model", key "model_id"). When those
profiles are installed on the host, supported ids are corroborated and newer
unrecognized models must remain fail-closed. Installing a newer slicer does
not expand ODIN's supported-model table.
"""
import json
import os
import sys
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
PROFILES = Path(os.environ.get('BAMBU_STUDIO_PROFILES',
    '/Applications/BambuStudio.app/Contents/Resources/profiles/BBL/machine' if sys.platform == 'darwin'
    else '/usr/share/bambu-studio/profiles/BBL/machine'))
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


def _assert_profiles_agree(profiles):
    from core.printer_identity import canonical_machine
    from modules.models_library.threemf_parser import _friendly_printer_name
    from modules.printers.printer_models import bambu_model_name, normalize_model_name
    found = {}
    for path in profiles.glob('Bambu Lab *.json'):
        data = json.loads(path.read_text())
        if data.get('type') == 'machine_model' and data.get('model_id'):
            model = data['name'].removeprefix('Bambu Lab ')
            name = FRIENDLY.get(model, model)
            code = data['model_id']
            assert code not in found or found[code] == name, f'Conflicting vendor model id: {code}'
            found[code] = name
    assert set(EXPECTED) <= set(found), f'Missing supported vendor ids: {sorted(set(EXPECTED) - set(found))}'
    assert {code: found[code] for code in EXPECTED} == EXPECTED
    for code in set(found) - set(EXPECTED):
        assert bambu_model_name(code) == code
        assert normalize_model_name('bambu', code) == code
        assert _friendly_printer_name(code) == code
        for identity in (code, found[code], 'Bambu Lab ' + found[code]):
            assert canonical_machine(identity) is None, f'Unverified vendor model became canonical: {identity}'


@pytest.mark.skipif(not PROFILES.is_dir(), reason='Bambu Studio profiles are not installed on this host')
def test_installed_bambu_studio_profiles_agree():
    _assert_profiles_agree(PROFILES)


@pytest.mark.parametrize('code,name', [('N6', 'X2D'), ('N9', 'A2L')])
def test_newer_vendor_models_remain_unrecognized(code, name):
    from core.printer_identity import canonical_machine
    from modules.models_library.threemf_parser import _friendly_printer_name
    from modules.printers.printer_models import bambu_model_name, normalize_model_name
    assert bambu_model_name(code) == normalize_model_name('bambu', code) == _friendly_printer_name(code) == code
    assert all(canonical_machine(value) is None for value in (code, name, 'Bambu Lab ' + name))


@pytest.mark.parametrize('damage', ['missing', 'renamed', 'duplicate'])
def test_profile_corroboration_preserves_supported_identity_checks(tmp_path, damage):
    for code, name in EXPECTED.items():
        (tmp_path / f'Bambu Lab {code}.json').write_text(json.dumps({'type': 'machine_model', 'model_id': code, 'name': 'Bambu Lab ' + name}))
    path = tmp_path / 'Bambu Lab BL-P001.json'
    if damage == 'missing':
        path.unlink()
    elif damage == 'renamed':
        path.write_text(json.dumps({'type': 'machine_model', 'model_id': 'BL-P001', 'name': 'Bambu Lab Wrong Model'}))
    else:
        (tmp_path / 'Bambu Lab conflicting.json').write_text(json.dumps({'type': 'machine_model', 'model_id': 'BL-P001', 'name': 'Bambu Lab Wrong Model'}))
    with pytest.raises(AssertionError):
        _assert_profiles_agree(tmp_path)
