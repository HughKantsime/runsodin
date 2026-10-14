"""Actual additive migration adoption, drift and caller rollback requirements."""
import importlib
import pytest
from sqlalchemy import create_engine,inspect,text

from tests.test_contracts.test_education_review_workflow import review_db

MODULE='core.schema.migrations.009_cost_foundation'

def test_cost_migration_is_additive_idempotent_and_does_not_invent_history(tmp_path):
    migration=importlib.import_module(MODULE)
    engine=create_engine(f"sqlite:///{tmp_path/'cost.db'}")
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE original_evidence(id INTEGER PRIMARY KEY,note TEXT)'))
        conn.execute(text("INSERT INTO original_evidence VALUES(1,'unchanged')"))
        migration.apply(conn)
        migration.validate(conn)
        conn.execute(text("INSERT INTO finance_currency_settings VALUES('org',1,NULL)"))
    with engine.begin() as conn:migration.apply(conn);migration.validate(conn)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT note FROM original_evidence')).scalar_one()=='unchanged'
        assert conn.execute(text('SELECT currency FROM finance_currency_settings')).scalar_one() is None
        assert conn.execute(text('SELECT COUNT(*) FROM cost_identities')).scalar_one()==0
    engine.dispose()


def test_partial_footprint_is_rejected_without_creating_remaining_tables(tmp_path):
    migration=importlib.import_module(MODULE)
    engine=create_engine(f"sqlite:///{tmp_path/'partial.db'}")
    with engine.begin() as conn:conn.execute(text('CREATE TABLE cost_identities(identity_uid TEXT)'))
    with pytest.raises(Exception,match='footprint|shape'):
        with engine.begin() as conn:migration.apply(conn)
    with engine.connect() as conn:assert inspect(conn).get_table_names()==['cost_identities']
    engine.dispose()


def test_caller_rollback_removes_new_cost_schema(tmp_path):
    migration=importlib.import_module(MODULE)
    engine=create_engine(f"sqlite:///{tmp_path/'rollback.db'}")
    with pytest.raises(RuntimeError,match='caller rollback'):
        with engine.begin() as conn:
            migration.apply(conn)
            raise RuntimeError('caller rollback')
    with engine.connect() as conn:assert inspect(conn).get_table_names()==[]
    engine.dispose()


def test_missing_immutable_guard_is_rejected_on_rerun(tmp_path):
    migration=importlib.import_module(MODULE)
    engine=create_engine(f"sqlite:///{tmp_path/'drift.db'}")
    with engine.begin() as conn:
        migration.apply(conn)
        conn.execute(text('DROP TRIGGER cost_identities_no_replace'))
    with pytest.raises(Exception,match='shape|guard|footprint'):
        with engine.begin() as conn:migration.apply(conn)
    engine.dispose()


@pytest.mark.parametrize('change',['currency','delete_action','unique','default','replacement_guard'])
def test_adoption_rejects_weakened_physical_contract(tmp_path,change):
    migration=importlib.import_module(MODULE)
    engine=create_engine(f"sqlite:///{tmp_path/(change+'.db')}")
    ddl=migration.SQLITE_SQL
    changes={
        'currency':("currency TEXT CHECK(currency IS NULL OR currency IN ('USD','EUR','GBP','CAD','AUD','JPY','CHF','NZD'))","currency TEXT"),
        'delete_action':('ON DELETE RESTRICT','ON DELETE CASCADE'),
        'unique':(' UNIQUE(source_uid,allocation_key),',''),
        'default':("DEFAULT 'order'","DEFAULT 'item'"),
        'replacement_guard':("SELECT RAISE(ABORT,'retained cost evidence cannot be replaced')","SELECT 1"),
    }
    old,new=changes[change]
    assert old in ddl
    raw=engine.raw_connection()
    try:raw.driver_connection.executescript(ddl.replace(old,new))
    finally:raw.close()
    with pytest.raises(Exception,match='shape'):
        with engine.begin() as conn:migration.apply(conn)
    with engine.connect() as conn:assert conn.execute(text('SELECT COUNT(*) FROM cost_identities')).scalar_one()==0
    engine.dispose()


def test_actual_bootstrap_creates_empty_cost_foundation_and_records_history(review_db):
    migration=importlib.import_module(MODULE)
    engine=review_db.get_bind()
    with engine.connect() as conn:
        migration.validate(conn)
        assert conn.execute(text("SELECT COUNT(*) FROM odin_schema_migrations WHERE migration_id='python:009-cost-foundation'")).scalar_one()==1
        for table in ('cost_identities','cost_obligations','cost_attempt_participation','finance_currency_settings'):
            assert conn.execute(text('SELECT COUNT(*) FROM '+table)).scalar_one()==0
