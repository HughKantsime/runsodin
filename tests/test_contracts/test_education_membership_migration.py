"""Real SQLite rebuild and PostgreSQL constraint swap, including retained history."""
import importlib
import pytest
from sqlalchemy import create_engine, event, inspect, text
from core.schema import bootstrap_database
from core.schema.migrator import apply_dedicated_python_migration
from tests.test_contracts.test_post027_migration_033_postgres import pg_engine

M = importlib.import_module('core.schema.migrations.037_education_memberships')


@pytest.fixture(params=['sqlite', 'postgresql'])
def engine(request,tmp_path):
    if request.param=='postgresql':
        return request.getfixturevalue('pg_engine')
    result=create_engine(f"sqlite:///{tmp_path/'membership.db'}")
    @event.listens_for(result,'connect')
    def fk(conn,_):conn.execute('PRAGMA foreign_keys=ON')
    request.addfinalizer(result.dispose)
    return result


def test_upgrade_preserves_objects_rows_and_revalidates(engine,monkeypatch):
    import core.schema.bootstrap as bootstrap
    original=bootstrap.POST027_MIGRATIONS
    monkeypatch.setattr(bootstrap,'POST027_MIGRATIONS',tuple(pair for pair in original if pair[0]!=M.MIGRATION_ID))
    # The dedicated final step is disabled while constructing the exact old schema.
    real=bootstrap.apply_dedicated_python_migration
    monkeypatch.setattr(bootstrap,'apply_dedicated_python_migration',lambda e,n: False if n.endswith('037_education_memberships') else real(e,n))
    required=bootstrap.RAW_REQUIRED_TABLES
    columns=bootstrap.RAW_REQUIRED_COLUMNS
    monkeypatch.setattr(bootstrap,'RAW_REQUIRED_TABLES',required-frozenset({'education_memberships'}))
    monkeypatch.setattr(bootstrap,'RAW_REQUIRED_COLUMNS',{k:v for k,v in columns.items() if k!='education_memberships'})
    bootstrap_database(engine)
    monkeypatch.setattr(bootstrap,'RAW_REQUIRED_TABLES',required)
    monkeypatch.setattr(bootstrap,'RAW_REQUIRED_COLUMNS',columns)
    with engine.begin() as c:
        c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'school',TRUE),(2,'other',TRUE)"))
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'stranded','fixture','admin',TRUE,1),(2,'other','fixture','viewer',TRUE,2)"))
        c.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,display_name,code_key,code,created_by) VALUES(1,1,'c','C','c','C',1)"))
        c.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) VALUES(1,1,1,'manager','active',1)"))
        c.execute(text("CREATE INDEX custom_retained_grant ON education_cost_center_grants(granted_by)"))
        before={t:inspect(c).get_columns(t) for t in M.PAIRS}
        if engine.dialect.name=='sqlite':
            c.exec_driver_sql("UPDATE sqlite_sequence SET seq=50000 WHERE name='education_cost_center_grants'")
            c.exec_driver_sql("CREATE TRIGGER custom_grant_marker AFTER UPDATE ON education_cost_center_grants BEGIN SELECT 1; END")
    assert apply_dedicated_python_migration(engine,M.__name__)
    assert not apply_dedicated_python_migration(engine,M.__name__)
    with engine.begin() as c:
        M.validate(c)
        assert {(r.user_id,r.org_id) for r in c.execute(text('SELECT * FROM education_memberships'))}=={(1,1),(2,2)}
        for t in M.PAIRS:
            assert [(x['name'],str(x['type']),x['nullable']) for x in inspect(c).get_columns(t)]==[(x['name'],str(x['type']),x['nullable']) for x in before[t]]
        assert 'custom_retained_grant' in {i['name'] for i in inspect(c).get_indexes('education_cost_center_grants')}
        c.execute(text('UPDATE users SET group_id=NULL WHERE id=1'))
        assert c.execute(text('SELECT user_id FROM education_cost_center_grants')).scalar_one()==1
        if engine.dialect.name=='sqlite':
            assert c.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name='education_cost_center_grants'").scalar()==50000
            assert c.exec_driver_sql('PRAGMA foreign_key_check').all()==[]
            assert c.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE name='custom_grant_marker'").scalar()==1
        else:
            assert c.execute(text("SELECT COUNT(*) FROM pg_constraint WHERE conname LIKE '%_retained' AND NOT convalidated")).scalar()==0
    with engine.begin() as c:c.execute(text('DROP INDEX custom_retained_grant'))
    monkeypatch.setattr(bootstrap,'POST027_MIGRATIONS',original)
    monkeypatch.setattr(bootstrap,'apply_dedicated_python_migration',real)
    assert bootstrap_database(engine)['applied']==[]


def test_partial_membership_schema_refused(tmp_path):
    e=create_engine(f"sqlite:///{tmp_path/'partial.db'}")
    with e.begin() as c:c.execute(text('CREATE TABLE education_memberships(user_id INTEGER)'))
    with pytest.raises(RuntimeError,match='columns differ'):
        apply_dedicated_python_migration(e,M.__name__)
    with e.connect() as c:assert c.exec_driver_sql('PRAGMA foreign_keys').scalar()==1
    e.dispose()


@pytest.mark.parametrize('table,column',[(t,c) for t,cols in M.PAIRS.items() for c in cols])
@pytest.mark.parametrize('orphan_kind',['missing_user','wrong_membership'])
def test_orphan_in_each_of_seven_sources_refuses_and_rolls_back(tmp_path,table,column,orphan_kind):
    e=create_engine(f"sqlite:///{tmp_path/'orphan.db'}")
    with e.begin() as c:
        c.exec_driver_sql('CREATE TABLE users(id INTEGER PRIMARY KEY,group_id INTEGER)')
        c.exec_driver_sql('CREATE TABLE groups(id INTEGER PRIMARY KEY)')
        if orphan_kind=='wrong_membership':
            c.exec_driver_sql('INSERT INTO groups(id) VALUES(1),(2)')
            c.exec_driver_sql('INSERT INTO users(id,group_id) VALUES(999,2)')
        for name,cols in M.PAIRS.items():
            c.exec_driver_sql(f'CREATE TABLE {name}(org_id INTEGER,'+','.join(col+' INTEGER' for col in cols)+')')
        c.exec_driver_sql(f'INSERT INTO {table}(org_id,{column}) VALUES(1,999)')
    with pytest.raises(RuntimeError,match='Orphan Education membership'):
        apply_dedicated_python_migration(e,M.__name__)
    assert not inspect(e).has_table('education_memberships')
    with e.connect() as c:assert c.exec_driver_sql('PRAGMA foreign_keys').scalar()==1
    e.dispose()


def test_rerun_refuses_missing_retention_trigger(tmp_path):
    e=create_engine(f"sqlite:///{tmp_path/'trigger.db'}")
    bootstrap_database(e)
    with e.begin() as c:c.exec_driver_sql('DROP TRIGGER trg_retained_education_submissions_approved_by_insert')
    with pytest.raises(RuntimeError,match='write triggers missing'):
        apply_dedicated_python_migration(e,M.__name__)
    e.dispose()
