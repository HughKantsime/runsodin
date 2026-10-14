"""Positive027 frozen profile oracle, not an activated historical builder."""
import hashlib,json,shutil
from pathlib import Path
import pytest
from core.schema import prepriced_restore_contract as contract
from core.schema.migrator import MigrationError

ROOT=Path(__file__).resolve().parents[2]/'backend'

def test_actual_capture_oracle_and_positive_registered_footprint():
    original=json.loads((ROOT.parent/'tests/fixtures/certification/prepriced027/catalogue.json').read_text())
    original['trigger_catalog']=[r for r in original['trigger_catalog'] if r[4] is False]
    contract.validate_snapshot(original)
    profile=contract.inventory()
    assert len(profile['schema'])==117
    assert len(profile['functions'])==21 and len(profile['triggers'])==31
    assert len(profile['trigger_catalog'])==31
    assert not any(name.startswith('priced_') or name.startswith('job_estimate_') for name in profile['schema'])
    ledger={row[0]:row for row in profile['ledger']}
    assert len(ledger)==52 and ledger['python:027-power-target-operations'][2]=='postgresql'
    assert not any(name.startswith('python:028') for name in ledger)
    from core.schema.bootstrap import RAW_REQUIRED_TABLES,RAW_REQUIRED_COLUMNS
    from modules.system.postgres_canonical_executables import canonical_functions,canonical_triggers
    assert profile['raw_tables']==sorted(RAW_REQUIRED_TABLES)
    assert profile['raw_columns']=={k:sorted(v) for k,v in RAW_REQUIRED_COLUMNS.items()}
    assert profile['functions']==json.loads(json.dumps(canonical_functions()))
    assert profile['triggers']==json.loads(json.dumps([[list(k),list(v)] for k,v in sorted(canonical_triggers().items())]))
    assert ledger['python:027-power-target-operations'][1]==hashlib.sha256((ROOT/'core/schema/migrations/027_power_target_operations.py').read_bytes()).hexdigest()
    contract.check_sources()

@pytest.mark.parametrize('key',['schema','ledger','functions','triggers','constraints','indexes','enums','trigger_catalog','raw_tables','raw_columns'])
def test_exact_catalogue_drift_refuses(key):
    snapshot=contract.inventory();snapshot[key]=None
    with pytest.raises(MigrationError,match='catalogue differs'):contract.validate_snapshot(snapshot)

@pytest.mark.parametrize('kind',['changed','new','missing'])
def test_sql_inventory_drift_refuses_before_any_database_work(tmp_path,kind):
    for name in contract.inventory()['sources']:
        target=tmp_path/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
    chosen=tmp_path/'core/migrations/001_initial.sql'
    if kind=='changed':chosen.write_bytes(chosen.read_bytes()+b'\n-- changed\n')
    elif kind=='new':(tmp_path/'core/migrations/999_unqualified.sql').write_text('SELECT 1;')
    else:chosen.unlink()
    with pytest.raises(MigrationError):contract.check_sources(tmp_path)

def test_inventory_cannot_mutate_frozen_profile():
    first=contract.inventory();first['ledger'].clear();first['schema'].clear()
    assert len(contract.inventory()['ledger'])==52 and len(contract.inventory()['schema'])==117
