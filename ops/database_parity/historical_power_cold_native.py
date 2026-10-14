"""Affected cold import/profile qualification only; prior restore receipt stays separate."""
import builtins,hashlib,json,os,stat,sys,uuid
from pathlib import Path
from sqlalchemy import create_engine,text
from sqlalchemy.engine import URL
private=Path(os.environ['HISTORICAL_NATIVE_PASSWORD_FILE'])
assert private.stat().st_uid==os.getuid()==10001 and stat.S_IMODE(private.stat().st_mode)==0o600
print('runtime-private-file uid=10001 mode=600',flush=True)
os.environ.update(DATABASE_URL='postgresql+psycopg://postgres@pg/postgres',DATABASE_PASSWORD_FILE=str(private),JWT_SECRET_KEY='owned-cold-profile-signing-key-only')
mode=os.environ['HISTORICAL_PROFILE_VERSION'];assert mode in ('25','26','helper')
admin=create_engine(URL.create('postgresql+psycopg',username='postgres',password=private.read_text(),host='pg',database='postgres'),isolation_level='AUTOCOMMIT',hide_parameters=True)
name='odin_validate_'+uuid.uuid4().hex[:20]
with admin.connect() as c:c.exec_driver_sql('CREATE DATABASE '+name)
engine=create_engine(admin.url.set(database=name),hide_parameters=True)
original_import=builtins.__import__;original_import_module=None;blocked=[]
def refuse_new(name,*args,**kwargs):
    if name in ('core.power_target_operation_schema','core.power_target_operations') or name.endswith('027_power_target_operations'):
        blocked.append(name);raise AssertionError('Historical path evaluated027 helper')
    return original_import(name,*args,**kwargs)
try:
    if mode!='helper':builtins.__import__=refuse_new
    else:
        # Fresh process: prove helper-first import reaches usable real PG factory.
        from core.power_target_operations import PowerFacts,_stage_operation
    from core.schema import bootstrap_database
    from core.schema.pre026_restore_contract import (PRE026_RESTORE025,PRE027_RESTORE026,owned_stage_database,bootstrap_stage,validate_inventory,validate_postgres_history,EXPECTED_POSTGRES_LEDGER,EXPECTED_POSTGRES_LEDGER026,SQL_SOURCE_PINS)
    from modules.system.postgres_canonical_executables import validate_executables,canonical_functions,canonical_triggers
    if mode!='helper':
        # importlib.import_module bypasses __import__ for directly named modules.
        import importlib
        original_import_module=importlib.import_module
        def guarded_module(name,*args,**kwargs):
            if name in ('core.power_target_operation_schema','core.power_target_operations') or name.endswith('027_power_target_operations'):
                blocked.append(name);raise AssertionError('Historical path evaluated027 helper')
            return original_import_module(name,*args,**kwargs)
        importlib.import_module=guarded_module
        contract=PRE026_RESTORE025 if mode=='25' else PRE027_RESTORE026
        stage=owned_stage_database(engine,name,contract);bootstrap_stage(stage)
        with engine.connect() as c:
            validate_inventory(c,contract);validate_postgres_history(c,contract);validate_executables(c,_contract=contract)
            actual_count=c.execute(text("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'")).scalar_one()
            trigger_count=c.execute(text("SELECT count(*) FROM pg_trigger t JOIN pg_class p ON p.oid=t.tgrelid JOIN pg_namespace n ON n.oid=p.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal")).scalar_one()
            assert (actual_count,trigger_count)==((17,24) if mode=='25' else (20,29))
            assert c.execute(text("SELECT count(*) FROM odin_schema_migrations")).scalar_one()==(50 if mode=='25' else 51)
            assert c.execute(text("SELECT to_regclass('public.power_target_operations')")).scalar_one() is None
        assert not blocked and all(n not in sys.modules for n in ('core.power_target_operation_schema','core.power_target_operations','core.schema.migrations.027_power_target_operations'))
        assert len(SQL_SOURCE_PINS)==24 and len(EXPECTED_POSTGRES_LEDGER)==50 and len(EXPECTED_POSTGRES_LEDGER026)==51
        builtins.__import__=original_import;importlib.import_module=original_import_module
    bootstrap_database(engine,Path('/source/backend'))
    with engine.begin() as c:
        validate_executables(c)
        assert (len(canonical_functions()),len(canonical_triggers()))==(21,31)
        assert c.execute(text("SELECT count(*) FROM odin_schema_migrations")).scalar_one()==52
        if mode=='helper':
            owner=_stage_operation(c,PowerFacts(printer_id=1,target_key='1'*64,config_key='2'*64,destination_key='3'*64,purpose='manual_on',request_nonce='4'*32,actor_user_id=1,auth_kind='bearer_jwt',authority_key='5'*64))
            assert c.execute(text('SELECT state FROM power_target_operations')).scalar_one()=='preparing'
            c.rollback()
    if mode=='helper':
        with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM power_target_operations')).scalar_one()==0
    print(json.dumps({'mode':mode,'historical027_module_absent':mode!='helper','fixed_sql_count':24,'historical_catalog':None if mode=='helper' else ([17,24] if mode=='25' else [20,29]),'current_catalog':[21,31],'current_ledger':52,'scope':'cold import/exact bootstrap profile and ordinary upgrade only; no repeated archive restore or command proof'}),flush=True)
finally:
    builtins.__import__=original_import
    if original_import_module is not None:importlib.import_module=original_import_module
    engine.dispose()
    with admin.connect() as c:c.exec_driver_sql('DROP DATABASE '+name+' WITH (FORCE)')
    admin.dispose()
print('historical-cold-'+mode+'-native-pass',flush=True)
