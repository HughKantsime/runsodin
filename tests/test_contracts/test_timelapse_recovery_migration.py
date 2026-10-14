"""Additive recovery evidence cannot infer identities for existing retirements."""
import importlib
import pytest
from sqlalchemy import create_engine,inspect,text


def migration():
    return importlib.import_module('core.schema.migrations.017_timelapse_recovery')


@pytest.fixture
def legacy_recovery_engine():
    engine=create_engine('sqlite:///:memory:')
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA foreign_keys=ON')
        db.exec_driver_sql('CREATE TABLE timelapses(id INTEGER PRIMARY KEY,printer_id INTEGER,filename TEXT,status TEXT)')
        db.exec_driver_sql("INSERT INTO timelapses VALUES(199,9,'owned.mp4','ready')")
        importlib.import_module('core.schema.migrations.015_timelapse_retirements').apply(db)
        db.exec_driver_sql("INSERT INTO timelapse_retirements(operation_id,timelapse_id,printer_id,owner_state,owner_org_id,filename,root_device,root_inode,state,revision) VALUES('owned',199,9,'known',1,'owned.mp4',1,100,'pending',1)")
        db.exec_driver_sql("INSERT INTO timelapse_retirement_objects(operation_id,object_index,relative_path,kind,device,inode,uid,gid,mode,link_count,byte_count,mtime_ns,ctime_ns,state,quarantine_name) VALUES('owned',0,'owned.mp4','file',1,101,501,20,33152,1,20,100,100,'planned','owned_0')")
    yield engine
    engine.dispose()


def test_recovery_fresh_repeat_preserves_legacy_without_inferred_facts(legacy_recovery_engine):
    module=migration()
    with legacy_recovery_engine.begin() as db:
        module.apply(db);module.apply(db);module.validate(db)
        for table in module.TABLES:assert db.execute(table.select()).all()==[]
        assert db.execute(text('SELECT filename,status,retirement_operation_id FROM timelapses')).one()==('owned.mp4','ready',None)
        assert db.execute(text('SELECT operation_id,state FROM timelapse_retirements')).one()==('owned','pending')


def test_recovery_caller_rollback_removes_only_new_tables(legacy_recovery_engine):
    module=migration()
    with pytest.raises(RuntimeError):
        with legacy_recovery_engine.begin() as db:
            module.apply(db);raise RuntimeError('owned transaction refusal')
    for table in module.TABLES:assert not inspect(legacy_recovery_engine).has_table(table.name)
    with legacy_recovery_engine.connect() as db:assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirements')).scalar_one()==1


def test_bad_adoption_refuses_before_creating_any_missing_table(legacy_recovery_engine):
    module=migration()
    with legacy_recovery_engine.begin() as db:db.exec_driver_sql('CREATE TABLE timelapse_retirement_parents(broken TEXT)')
    with pytest.raises(Exception):
        with legacy_recovery_engine.begin() as db:module.apply(db)
    for table in module.TABLES[1:]:assert not inspect(legacy_recovery_engine).has_table(table.name)


@pytest.mark.parametrize('change',[
    {'kind':'file','digest_sha256':None},
    {'digest_sha256':'a'*63},
    {'digest_sha256':'A'*64},
    {'digest_sha256':'g'*64},
    {'kind':'directory','digest_sha256':'a'*64},
    {'phase':'staged','verified_ctime_ns':None},
    {'phase':'planned','verified_ctime_ns':1},
    {'phase':'rename_pending','verified_ctime_ns':1},
    {'phase':'guessed'},
    {'object_index':1},
    {'operation_id':'missing'},
])
def test_recovery_move_invalid_evidence_or_missing_object_refuses(legacy_recovery_engine,change):
    module=migration()
    with legacy_recovery_engine.begin() as db:module.apply(db)
    from core.timelapse_recovery_schema import moves
    row={'operation_id':'owned','object_index':0,'kind':'file','digest_sha256':'a'*64,'phase':'planned','verified_ctime_ns':None};row.update(change)
    with pytest.raises(Exception):
        with legacy_recovery_engine.begin() as db:db.execute(moves.insert(),row)


def test_recovery_move_progress_retains_bound_digest(legacy_recovery_engine):
    module=migration()
    from core.timelapse_recovery_schema import moves
    with legacy_recovery_engine.begin() as db:
        module.apply(db)
        db.execute(moves.insert(),{'operation_id':'owned','object_index':0,'kind':'file','digest_sha256':'a'*64,'phase':'planned'})
        db.execute(moves.update().values(phase='rename_pending'))
        db.execute(moves.update().values(phase='staged',verified_ctime_ns=2**55))
        row=db.execute(moves.select()).mappings().one()
        assert row['digest_sha256']=='a'*64 and row['verified_ctime_ns']==2**55
        with pytest.raises(Exception):db.execute(text("DELETE FROM timelapse_retirement_objects WHERE operation_id='owned'"))


@pytest.mark.parametrize('change',[{'inode':0},{'device':-1},{'uid':-1},{'mode':33152},{'mode':16895},{'operation_id':'missing'}])
def test_recovery_directory_evidence_invalid_shape_refuses(legacy_recovery_engine,change):
    module=migration()
    from core.timelapse_recovery_schema import parents,quarantine
    with legacy_recovery_engine.begin() as db:module.apply(db)
    values={'operation_id':'owned','device':1,'inode':102,'uid':501,'gid':20,'mode':16832};values.update(change)
    for table in (parents,quarantine):
        with pytest.raises(Exception):
            with legacy_recovery_engine.begin() as db:db.execute(table.insert(),dict(values,relative_path='') if table is parents else values)


def test_recovery_unexpected_index_refuses_recorded_adoption(legacy_recovery_engine):
    module=migration()
    with legacy_recovery_engine.begin() as db:
        module.apply(db);db.exec_driver_sql('CREATE INDEX unexpected_recovery ON timelapse_retirement_moves(phase)')
    with pytest.raises(Exception):
        with legacy_recovery_engine.begin() as db:module.validate(db)


@pytest.mark.parametrize('absent',[False,True])
def test_recovery_adoption_rejects_wrong_kind_or_absent_source(legacy_recovery_engine,absent):
    module=migration()
    from core.timelapse_recovery_schema import moves
    with legacy_recovery_engine.begin() as db:
        module.apply(db)
        if absent:
            db.exec_driver_sql("UPDATE timelapse_retirement_objects SET state='originally_absent',quarantine_name=NULL,device=NULL,inode=NULL,uid=NULL,gid=NULL,mode=NULL,link_count=NULL,byte_count=NULL,mtime_ns=NULL,ctime_ns=NULL")
            kind='file';digest='a'*64
        else:kind='directory';digest=None
        db.execute(moves.insert(),{'operation_id':'owned','object_index':0,'kind':kind,'digest_sha256':digest,'phase':'planned'})
    with pytest.raises(Exception):
        with legacy_recovery_engine.begin() as db:module.validate(db)


def test_root_and_quarantine_stable_facts_are_retained(legacy_recovery_engine):
    module=migration()
    from core.timelapse_recovery_schema import parents,quarantine
    values={'operation_id':'owned','device':2**40,'inode':2**45,'uid':501,'gid':20,'mode':16832}
    with legacy_recovery_engine.begin() as db:
        module.apply(db);db.execute(parents.insert(),dict(values,relative_path=''));db.execute(quarantine.insert(),dict(values,inode=2**45+1))
        assert db.execute(parents.select()).mappings().one()['device']==2**40
        assert db.execute(quarantine.select()).mappings().one()['inode']==2**45+1
        module.validate(db)


@pytest.mark.parametrize('old,new',[
    ('device BIGINT NOT NULL','device INTEGER NOT NULL'),
    ('device BIGINT NOT NULL','device BIGINT DEFAULT 0 NOT NULL'),
    ('device BIGINT NOT NULL','device BIGINT'),
    ('PRIMARY KEY (operation_id, relative_path)','PRIMARY KEY (operation_id)'),
    ('ON DELETE RESTRICT','ON DELETE CASCADE'),
    ('ON DELETE RESTRICT','ON DELETE RESTRICT ON UPDATE CASCADE'),
])
def test_adoption_shape_drift_refuses_before_missing_tables(legacy_recovery_engine,old,new):
    module=migration()
    from core.timelapse_recovery_schema import parents,moves
    from sqlalchemy.schema import CreateTable
    sql=str(CreateTable(parents).compile(dialect=legacy_recovery_engine.dialect));assert old in sql
    with legacy_recovery_engine.begin() as db:db.exec_driver_sql(sql.replace(old,new))
    with pytest.raises(Exception):
        with legacy_recovery_engine.begin() as db:module.apply(db)
    assert not inspect(legacy_recovery_engine).has_table(moves.name)
