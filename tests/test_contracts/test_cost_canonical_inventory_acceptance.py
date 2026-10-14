"""Full bootstrap physical inventory must be accepted by native backup inventory."""
from pathlib import Path
from sqlalchemy import create_engine,inspect
from core.schema import bootstrap_database
from core.schema.bootstrap import RAW_REQUIRED_COLUMNS
from modules.system.postgres_backup_service import canonical_tables

def test_cost_foundation_is_in_full_canonical_inventory(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'owned-cost-inventory.db'))
    try:
        bootstrap_database(engine,Path(__file__).resolve().parents[2]/'backend')
        physical=inspect(engine)
        assert set(physical.get_table_names())==set(canonical_tables())
        cost_tables={'cost_identities','cost_order_memberships','cost_job_bindings','cost_attempt_bindings','cost_obligations','cost_attempt_participation','finance_currency_settings'}
        for name in cost_tables:
            assert name in canonical_tables()
            assert {c['name'] for c in physical.get_columns(name)}==RAW_REQUIRED_COLUMNS[name]
        before=set(physical.get_table_names())
        result=bootstrap_database(engine,Path(__file__).resolve().parents[2]/'backend')
        assert result['applied']==[]
        assert set(inspect(engine).get_table_names())==before
    finally:engine.dispose()
