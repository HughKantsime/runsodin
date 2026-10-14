"""Durable owner nonce contract; no retrospective tenant inference or media I/O."""
import importlib
import pytest
from sqlalchemy import create_engine,inspect,text

@pytest.fixture
def legacy_owner_engine():
    engine=create_engine('sqlite:///:memory:')
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE timelapses(id INTEGER PRIMARY KEY,printer_id INTEGER NOT NULL,print_job_id INTEGER,filename TEXT NOT NULL,status TEXT NOT NULL)')
        db.exec_driver_sql("INSERT INTO timelapses VALUES(199,9,444,'owned.mp4','ready')")
    yield engine
    engine.dispose()


def ownership_migration():
    return importlib.import_module('core.schema.migrations.016_timelapse_ownership')


def test_owner_migration_preserves_unanchored_legacy_and_repeat(legacy_owner_engine):
    module=ownership_migration()
    with legacy_owner_engine.begin() as db:
        module.apply(db);module.apply(db);module.validate(db)
        assert db.execute(text('SELECT id,printer_id,print_job_id,filename,status,ownership_operation_id FROM timelapses')).one()==(199,9,444,'owned.mp4','ready',None)
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_ownership_anchors')).scalar_one()==0


def test_owner_migration_caller_rollback_is_atomic(legacy_owner_engine):
    module=ownership_migration()
    with pytest.raises(RuntimeError):
        with legacy_owner_engine.begin() as db:
            module.apply(db)
            raise RuntimeError('owned refusal')
    assert not inspect(legacy_owner_engine).has_table('timelapse_ownership_anchors')
    assert 'ownership_operation_id' not in {v['name'] for v in inspect(legacy_owner_engine).get_columns('timelapses')}


def test_owner_anchor_outlives_reused_live_id(legacy_owner_engine):
    module=ownership_migration()
    from core.timelapse_ownership_schema import anchors
    with legacy_owner_engine.begin() as db:
        module.apply(db)
        db.execute(anchors.insert(),{'operation_id':'original-owner','timelapse_id':199,'printer_id':9,'scheduled_job_id':444,'filename':'owned.mp4','owner_state':'known','owner_org_id':2,'ownership_source':'first_capture_job_file','print_file_id':111,'capture_printer_org_id':2,'capture_printer_shared':False})
        db.execute(text("UPDATE timelapses SET ownership_operation_id='original-owner' WHERE id=199"))
        db.execute(text('DELETE FROM timelapses WHERE id=199'))
        db.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status) VALUES(199,9,444,'owned.mp4','ready')"))
        assert db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=199')).scalar_one() is None
        assert db.execute(text('SELECT operation_id FROM timelapse_ownership_anchors')).scalar_one()=='original-owner'


@pytest.mark.parametrize('owner',[
 {'owner_state':'known','owner_org_id':None},
 {'owner_state':'known','owner_org_id':0},
 {'owner_state':'shared','owner_org_id':2},
 {'owner_state':'unresolved','owner_org_id':2},
 {'owner_state':'guessed','owner_org_id':2},
])
def test_owner_anchor_invalid_tenant_shape_refuses(legacy_owner_engine,owner):
    module=ownership_migration()
    from core.timelapse_ownership_schema import anchors
    with legacy_owner_engine.begin() as db:module.apply(db)
    values={'operation_id':'owned','timelapse_id':199,'printer_id':9,'scheduled_job_id':444,'filename':'owned.mp4','owner_state':'known','owner_org_id':2,'ownership_source':'first_capture_job_file','print_file_id':111,'capture_printer_org_id':2,'capture_printer_shared':False}
    values.update(owner)
    with pytest.raises(Exception):
        with legacy_owner_engine.begin() as db:db.execute(anchors.insert(),values)


def test_malformed_owner_adoption_preserves_legacy_without_new_nonce(legacy_owner_engine):
    module=ownership_migration()
    with legacy_owner_engine.begin() as db:db.exec_driver_sql('CREATE TABLE timelapse_ownership_anchors(broken TEXT)')
    with pytest.raises(Exception):
        with legacy_owner_engine.begin() as db:module.apply(db)
    assert 'ownership_operation_id' not in {v['name'] for v in inspect(legacy_owner_engine).get_columns('timelapses')}


@pytest.mark.parametrize('column_definition',['INTEGER',"VARCHAR(64) NOT NULL DEFAULT 'invented'"])
def test_malformed_owner_nonce_refuses_before_new_anchor(legacy_owner_engine,column_definition):
    module=ownership_migration()
    with legacy_owner_engine.begin() as db:db.exec_driver_sql('ALTER TABLE timelapses ADD COLUMN ownership_operation_id '+column_definition)
    with pytest.raises(Exception):
        with legacy_owner_engine.begin() as db:module.apply(db)
    assert not inspect(legacy_owner_engine).has_table('timelapse_ownership_anchors')


@pytest.mark.parametrize('change',[
 {'capture_printer_org_id':None},
 {'capture_printer_org_id':1},
 {'owner_state':'shared','owner_org_id':None,'capture_printer_org_id':None},
 {'owner_state':'shared','owner_org_id':None,'capture_printer_shared':True,'education_submission_id':7},
 {'education_submission_id':-1},
 {'ownership_source':'current_printer_guess'},
 {'scheduled_job_id':None},
 {'print_file_id':None},
])
def test_capture_provenance_cannot_publish_unknown_or_private_media(legacy_owner_engine,change):
    module=ownership_migration()
    from core.timelapse_ownership_schema import anchors
    with legacy_owner_engine.begin() as db:module.apply(db)
    values={'operation_id':'owned','timelapse_id':199,'printer_id':9,'scheduled_job_id':444,'filename':'owned.mp4','owner_state':'known','owner_org_id':2,'ownership_source':'first_capture_job_file','print_file_id':111,'capture_printer_org_id':2,'capture_printer_shared':False}
    values.update(change)
    with pytest.raises(Exception):
        with legacy_owner_engine.begin() as db:db.execute(anchors.insert(),values)


@pytest.mark.parametrize('field,value',[('filename','different.mp4'),('printer_id',10),('print_job_id',445)])
def test_populated_owner_nonce_requires_same_live_tuple(legacy_owner_engine,field,value):
    module=ownership_migration()
    from core.timelapse_ownership_schema import anchors
    with legacy_owner_engine.begin() as db:
        module.apply(db)
        db.execute(anchors.insert(),{'operation_id':'owned','timelapse_id':199,'printer_id':9,'scheduled_job_id':444,'filename':'owned.mp4','owner_state':'known','owner_org_id':2,'ownership_source':'first_capture_job_file','print_file_id':111,'capture_printer_org_id':2,'capture_printer_shared':False})
        db.execute(text("UPDATE timelapses SET ownership_operation_id='owned' WHERE id=199"))
        if field=='filename':db.execute(text('UPDATE timelapses SET filename=:value WHERE id=199'),{'value':value})
        elif field=='printer_id':db.execute(text('UPDATE timelapses SET printer_id=:value WHERE id=199'),{'value':value})
        else:db.execute(text('UPDATE timelapses SET print_job_id=:value WHERE id=199'),{'value':value})
    with pytest.raises(Exception):
        with legacy_owner_engine.begin() as db:module.validate(db)


@pytest.mark.parametrize('drift',['column_type','nullable','default','missing_check','extra_index'])
def test_adopted_owner_contract_drift_refuses_before_live_nonce_mutation(legacy_owner_engine,drift):
    from sqlalchemy import MetaData,String,Index,CheckConstraint
    from core.timelapse_ownership_schema import anchors
    module=ownership_migration();bad=anchors.to_metadata(MetaData())
    if drift=='column_type':bad.c.owner_org_id.type=String(16)
    elif drift=='nullable':bad.c.owner_state.nullable=True
    elif drift=='default':
        from sqlalchemy import DefaultClause
        bad.c.owner_state.server_default=DefaultClause('known')
    elif drift=='missing_check':
        for constraint in list(bad.constraints):
            if isinstance(constraint,CheckConstraint) and constraint.name=='to_owner':bad.constraints.remove(constraint)
    else:Index('owned_extra_index',bad.c.timelapse_id)
    with legacy_owner_engine.begin() as db:bad.create(db)
    with pytest.raises(Exception):
        with legacy_owner_engine.begin() as db:module.apply(db)
    assert 'ownership_operation_id' not in {v['name'] for v in inspect(legacy_owner_engine).get_columns('timelapses')}


def test_standalone_owner_validation_requires_pinned_helper_dependency(legacy_owner_engine,monkeypatch):
    from core.schema.migrator import MigrationError
    module=ownership_migration()
    monkeypatch.setattr(module,'HELPER_SOURCE_SHA256','0'*64)
    with pytest.raises(MigrationError,match='Media ownership helper contract changed'):
        with legacy_owner_engine.begin() as db:module.apply(db)
    assert not inspect(legacy_owner_engine).has_table('timelapse_ownership_anchors')
    assert 'ownership_operation_id' not in {v['name'] for v in inspect(legacy_owner_engine).get_columns('timelapses')}
