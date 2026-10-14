"""Additive retirement schema acceptance, not runtime/file deletion certification."""
import importlib
import pytest
from sqlalchemy import create_engine, inspect, text

@pytest.fixture
def legacy_engine():
    engine=create_engine('sqlite:///:memory:')
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE timelapses(id INTEGER PRIMARY KEY, filename TEXT NOT NULL, status TEXT NOT NULL)')
        db.exec_driver_sql("INSERT INTO timelapses VALUES(7,'original.mp4','ready')")
    yield engine
    engine.dispose()

def migration():return importlib.import_module('core.schema.migrations.015_timelapse_retirements')

def test_empty_additive_schema_and_repeat_preserve_original_media_metadata(legacy_engine):
    with legacy_engine.begin() as db:
        migration().apply(db);migration().apply(db);migration().validate(db)
        assert db.execute(text('SELECT id,filename,status,retirement_operation_id FROM timelapses')).one()==(7,'original.mp4','ready',None)
        for table in ['timelapse_retirements','timelapse_retirement_objects']:
            assert db.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one()==0

def test_caller_rollback_removes_new_tables_and_column(legacy_engine):
    with pytest.raises(RuntimeError):
        with legacy_engine.begin() as db:
            migration().apply(db)
            raise RuntimeError('Owned migration refused')
    assert not inspect(legacy_engine).has_table('timelapse_retirements')
    assert 'retirement_operation_id' not in {c['name'] for c in inspect(legacy_engine).get_columns('timelapses')}

@pytest.mark.parametrize('table',['timelapse_retirements','timelapse_retirement_objects'])
def test_malformed_adoption_refuses_before_additive_mutation(legacy_engine,table):
    with legacy_engine.begin() as db:db.exec_driver_sql(f'CREATE TABLE {table}(broken TEXT)')
    module=migration()
    with pytest.raises(Exception):
        with legacy_engine.begin() as db:module.apply(db)
    other='timelapse_retirement_objects' if table=='timelapse_retirements' else 'timelapse_retirements'
    assert not inspect(legacy_engine).has_table(other)
    assert 'retirement_operation_id' not in {c['name'] for c in inspect(legacy_engine).get_columns('timelapses')}

@pytest.mark.parametrize('definition',['INTEGER','VARCHAR(64) NOT NULL DEFAULT \'invented\''])
def test_existing_bad_row_nonce_refuses_before_new_tables(legacy_engine,definition):
    with legacy_engine.begin() as db:db.exec_driver_sql('ALTER TABLE timelapses ADD COLUMN retirement_operation_id '+definition)
    module=migration()
    with pytest.raises(Exception):
        with legacy_engine.begin() as db:module.apply(db)
    assert not inspect(legacy_engine).has_table('timelapse_retirements')

def test_objects_use_bigint_identity_not_truncated_integer(legacy_engine):
    with legacy_engine.begin() as db:migration().apply(db)
    columns={c['name']:c for c in inspect(legacy_engine).get_columns('timelapse_retirement_objects')}
    for name in ['device','inode','byte_count','mtime_ns','ctime_ns']:
        assert str(columns[name]['type']).upper()=='BIGINT'

def intent_values(operation='owned-a'):
    return {'operation_id':operation,'timelapse_id':7,'printer_id':9,'owner_state':'unresolved','filename':'original.mp4','root_device':0,'root_inode':2,'state':'pending','revision':1}

def object_values():
    return {'operation_id':'owned-a','object_index':0,'relative_path':'original.mp4','kind':'file','device':1,'inode':2**40,'uid':0,'gid':0,'mode':0o100600,'link_count':1,'byte_count':4,'mtime_ns':1800000000000000000,'ctime_ns':1800000000000000000,'state':'planned','quarantine_name':'owned-a-0'}

@pytest.mark.parametrize('invalid',[
    {'state':'originally_absent'},{'inode':None},{'inode':-1},{'byte_count':-1},
    {'ctime_ns':-1},{'quarantine_name':None},{'object_index':-1},
    {'kind':'symlink'},{'state':'guessed'},{'state':'blocked','reason_code':None},
    {'reason_code':'arbitrary-error-text'},
])
def test_object_contract_rejects_unproved_or_invalid_identity(legacy_engine,invalid):
    from sqlalchemy.exc import IntegrityError
    from core.timelapse_retirement_schema import intent,objects
    with legacy_engine.begin() as db:
        migration().apply(db);db.execute(intent.insert(),intent_values())
    with pytest.raises(IntegrityError):
        with legacy_engine.begin() as db:db.execute(objects.insert(),{**object_values(),**invalid})

def test_large_facts_and_explicit_absence_are_exact(legacy_engine):
    from core.timelapse_retirement_schema import intent,objects,facts
    with legacy_engine.begin() as db:
        migration().apply(db);db.execute(intent.insert(),intent_values())
        db.execute(objects.insert(),object_values())
        absent={**object_values(),'object_index':1,'relative_path':'missing.mp4','state':'originally_absent','quarantine_name':None,**{name:None for name in facts}}
        db.execute(objects.insert(),absent)
        assert db.execute(text('SELECT inode,mtime_ns FROM timelapse_retirement_objects WHERE object_index=0')).one()==(2**40,1800000000000000000)
        assert db.execute(text('SELECT inode,quarantine_name FROM timelapse_retirement_objects WHERE object_index=1')).one()==(None,None)

@pytest.mark.parametrize('duplicate',['relative_path','quarantine_name'])
def test_manifest_names_are_unique(legacy_engine,duplicate):
    from sqlalchemy.exc import IntegrityError
    from core.timelapse_retirement_schema import intent,objects
    with legacy_engine.begin() as db:
        migration().apply(db);db.execute(intent.insert(),intent_values());db.execute(objects.insert(),object_values())
    changed={**object_values(),'object_index':1,'relative_path':'other.mp4','quarantine_name':'owned-a-1'}
    changed[duplicate]=object_values()[duplicate]
    with pytest.raises(IntegrityError):
        with legacy_engine.begin() as db:db.execute(objects.insert(),changed)

@pytest.mark.parametrize('invalid',[{'owner_state':'known'},{'owner_state':'shared','owner_org_id':1},{'revision':0},{'state':'complete'},{'state':'blocked'}])
def test_intent_contract_refuses_fabricated_ownership_or_completion(legacy_engine,invalid):
    from sqlalchemy.exc import IntegrityError
    from core.timelapse_retirement_schema import intent
    with legacy_engine.begin() as db:migration().apply(db)
    with pytest.raises(IntegrityError):
        with legacy_engine.begin() as db:db.execute(intent.insert(),{**intent_values(),**invalid})

def test_populated_nonce_without_intent_refuses_before_new_tables(legacy_engine):
    with legacy_engine.begin() as db:
        db.exec_driver_sql('ALTER TABLE timelapses ADD COLUMN retirement_operation_id VARCHAR(64)')
        db.exec_driver_sql("UPDATE timelapses SET retirement_operation_id='unknown' WHERE id=7")
    module=migration()
    with pytest.raises(Exception):
        with legacy_engine.begin() as db:module.apply(db)
    assert not inspect(legacy_engine).has_table('timelapse_retirements')

@pytest.mark.parametrize('printer_id,status',[(10,'deleting'),(9,None),(None,'deleting')])
def test_live_nonce_requires_same_known_printer_and_explicit_retiring_status(printer_id,status):
    from core.timelapse_retirement_schema import intent
    from core.schema.migrator import MigrationError
    engine=create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as db:
            db.exec_driver_sql('CREATE TABLE timelapses(id INTEGER PRIMARY KEY,printer_id INTEGER,filename TEXT NOT NULL,status TEXT,retirement_operation_id VARCHAR(64))')
            db.execute(text("INSERT INTO timelapses VALUES(7,:printer,'original.mp4',:status,'owned-a')"),{'printer':printer_id,'status':status})
            intent.create(db);db.execute(intent.insert(),intent_values())
        with pytest.raises(MigrationError):
            with engine.begin() as db:migration().apply(db)
        assert not inspect(engine).has_table('timelapse_retirement_objects')
    finally:engine.dispose()
