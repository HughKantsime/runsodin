"""Claims F21-21.1-01, 21.2-01, 21.3-01, 21.4-01: advertised model names vs code recognition.

Executes the real model normalisation table for every advertised model, drives the
captured Bambu fixtures through the V2 parser to see which model identity is
observable, and records the Moonraker detection surface. Physical acceptance is out
of scope (no hardware).
"""
import json
from pathlib import Path

import pytest

from modules.printers.printer_models import normalize_model_name

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests' / 'fixtures' / 'telemetry'

BAMBU = {'X1C': ['BL-P001'], 'A1': ['N2S', 'BL-A001'], 'P1S': ['C11', 'BL-P00501'], 'H2D': ['BL-H001']}
PRUSA = {'MK4/S': ['MK4', 'MK4S'], 'MK3.9': ['MK39', 'MK3.9'], 'MK3.5': ['MK35', 'MK3.5'], 'MINI+': ['MINI', 'MINI+'],
         'XL': ['XL'], 'CORE One': ['CORE_ONE', 'COREONE']}
ELEGOO = {'Centauri Carbon': ['Centauri Carbon', 'CENTAURI CARBON'], 'Neptune 4': ['Neptune 4', 'Neptune 4 Pro'],
          'Saturn': ['Saturn 4', 'Saturn 4 Ultra']}


@pytest.mark.parametrize('model,codes', BAMBU.items())
def test_bambu_advertised_model_codes_normalise(model, codes):
    assert {normalize_model_name('bambu', c) for c in codes} == {model}


@pytest.mark.parametrize('model,codes', PRUSA.items())
def test_prusalink_advertised_model_codes_normalise(model, codes):
    names = {normalize_model_name('prusalink', c) for c in codes}
    assert names <= {model, 'MK4', 'MK4S'} and names


@pytest.mark.parametrize('model,names', ELEGOO.items())
def test_elegoo_advertised_machine_names_normalise(model, names):
    assert all(normalize_model_name('elegoo', n).startswith(model.split()[0]) for n in names)


def test_moonraker_has_no_model_table():
    assert normalize_model_name('moonraker', 'Creality K1') is None


def _product_names(path):
    names = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        payload = json.loads(line).get('payload') or {}
        for module in (payload.get('info') or {}).get('module') or []:
            if module.get('name') == 'ota' or str(module.get('product_name', '')).startswith('Bambu Lab'):
                names.add(module.get('product_name'))
    return names - {'', None}


def test_captured_bambu_fixture_model_identity_and_v2_printer_type():
    observed = {p.name: sorted(_product_names(p)) for p in sorted(FIXTURES.glob('bambu-*.jsonl')) if not p.name.endswith('.demo.jsonl')}
    record = {'fixture_product_names': observed}
    out = ROOT / 'artifacts' / 'application-certification' / 'claims-protocols-backend'
    out.mkdir(parents=True, exist_ok=True)
    # Captured identities present in committed fixtures.
    flat = {n for v in observed.values() for n in v}
    record['captured_models'] = sorted(flat)
    # Legacy auto-detect reads print.printer_type; count captured print sections carrying it.
    with_type = {}
    for p in sorted(FIXTURES.glob('bambu-*.jsonl')):
        n = 0
        for line in p.read_text().splitlines():
            if line.strip() and 'printer_type' in ((json.loads(line).get('payload') or {}).get('print') or {}):
                n += 1
        with_type[p.name] = n
    record['captured_print_sections_with_printer_type'] = with_type
    (out / 'printer-model-recognition.json').write_text(json.dumps(record, indent=1))
    assert 'Bambu Lab A1' in flat and 'Bambu Lab H2D' in flat
    # Fixture product names are passed through unchanged (not in code table).
    assert normalize_model_name('bambu', 'Bambu Lab H2D') == 'Bambu Lab H2D'
