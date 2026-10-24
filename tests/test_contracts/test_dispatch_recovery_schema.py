"""Additive schema adoption and append-only enforcement test-plan evidence."""
import importlib
import pytest
from sqlalchemy import create_engine, text

migration=importlib.import_module('core.schema.migrations.038_dispatch_recovery')

@pytest.mark.parametrize('weakening',['trigger','check','nullable'])
def test_adoption_refuses_weakened_recovery_schema(tmp_path,weakening):
    engine=create_engine('sqlite:///'+str(tmp_path/'history.db'))
    with engine.begin() as conn:
        migration.apply(conn)
        if weakening=='trigger':
            conn.exec_driver_sql('DROP TRIGGER recovery_no_update')
        else:
            schema=conn.execute(text("SELECT sql FROM sqlite_master WHERE name='dispatch_recovery_history'")).scalar_one()
            conn.exec_driver_sql('DROP TABLE dispatch_recovery_history')
            if weakening=='check': schema=schema.replace("decision IN ('hold','reconcile-retain','release')",'1=1')
            else: schema=schema.replace('actor_identity VARCHAR(128) NOT NULL','actor_identity VARCHAR(128)')
            conn.exec_driver_sql(schema)
        with pytest.raises(RuntimeError): migration.apply(conn)
    engine.dispose()
