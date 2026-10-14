"""Durable deletion receipts must be additive, retained and caller-atomic."""
import importlib
import pytest
from sqlalchemy import create_engine, inspect, text


def migration():
    return importlib.import_module('core.schema.migrations.018_timelapse_unlinks')


@pytest.fixture
def owned_unlink_legacy():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA foreign_keys=ON')
        db.exec_driver_sql('CREATE TABLE timelapses(id INTEGER PRIMARY KEY,printer_id INTEGER,filename TEXT,status TEXT)')
        db.exec_driver_sql("INSERT INTO timelapses VALUES(199,9,'owned.mp4','ready')")
        importlib.import_module('core.schema.migrations.015_timelapse_retirements').apply(db)
        db.execute(text("INSERT INTO timelapse_retirements(operation_id,timelapse_id,printer_id,owner_state,owner_org_id,filename,root_device,root_inode,state,revision) VALUES('owned',199,9,'known',1,'owned.mp4',1,100,'pending',1)"))
        db.execute(text("INSERT INTO timelapse_retirement_objects(operation_id,object_index,relative_path,kind,device,inode,uid,gid,mode,link_count,byte_count,mtime_ns,ctime_ns,state,quarantine_name) VALUES('owned',0,'owned.mp4','file',1,101,501,20,33152,1,20,100,100,'planned','owned_0')"))
        importlib.import_module('core.schema.migrations.017_timelapse_recovery').apply(db)
    yield engine
    engine.dispose()


def test_unlink_schema_fresh_repeat_never_backfills_deletion_authority(owned_unlink_legacy):
    module = migration()
    with owned_unlink_legacy.begin() as db:
        module.apply(db)
        module.apply(db)
        module.validate(db)
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirement_unlinks')).scalar_one() == 0
        assert db.execute(text('SELECT operation_id,state FROM timelapse_retirements')).one() == ('owned','pending')
        assert db.execute(text('SELECT relative_path,state FROM timelapse_retirement_objects')).one() == ('owned.mp4','planned')
        assert db.execute(text('SELECT filename,status FROM timelapses')).one() == ('owned.mp4','ready')


def test_unlink_schema_caller_rollback_preserves_originals(owned_unlink_legacy):
    module = migration()
    with pytest.raises(RuntimeError, match='owned rollback'):
        with owned_unlink_legacy.begin() as db:
            module.apply(db)
            raise RuntimeError('owned rollback')
    assert not inspect(owned_unlink_legacy).has_table('timelapse_retirement_unlinks')
    with owned_unlink_legacy.connect() as db:
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirement_objects')).scalar_one() == 1


@pytest.mark.parametrize('phase',['unlink_pending','unlink_confirmed','fsync_confirmed','retired'])
def test_unlink_receipt_valid_phase_retains_exact_original_authority(owned_unlink_legacy,phase):
    module = migration()
    with owned_unlink_legacy.begin() as db:
        module.apply(db)
        db.execute(text('INSERT INTO timelapse_retirement_unlinks(operation_id,object_index,phase) VALUES(:operation,0,:phase)'),{'operation':'owned','phase':phase})
        assert db.execute(text('SELECT operation_id,object_index,phase FROM timelapse_retirement_unlinks')).one() == ('owned',0,phase)
        with pytest.raises(Exception):
            db.execute(text("DELETE FROM timelapse_retirement_objects WHERE operation_id='owned'"))


@pytest.mark.parametrize('operation,index,phase',[
    ('missing',0,'unlink_pending'),('owned',1,'unlink_pending'),
    ('owned',-1,'unlink_pending'),('owned',0,'guessed'),('owned',0,None),
])
def test_unlink_receipt_missing_source_or_invalid_phase_refuses(owned_unlink_legacy,operation,index,phase):
    module = migration()
    with owned_unlink_legacy.begin() as db:
        module.apply(db)
    with pytest.raises(Exception):
        with owned_unlink_legacy.begin() as db:
            db.execute(text('INSERT INTO timelapse_retirement_unlinks(operation_id,object_index,phase) VALUES(:operation,:index,:phase)'),{'operation':operation,'index':index,'phase':phase})
    with owned_unlink_legacy.connect() as db:
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirement_unlinks')).scalar_one() == 0
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirement_objects')).scalar_one() == 1


@pytest.mark.parametrize('old,new',[
    ('object_index INTEGER NOT NULL','object_index BIGINT NOT NULL'),
    ('phase VARCHAR(24) NOT NULL',"phase VARCHAR(24) DEFAULT 'retired' NOT NULL"),
    ('phase VARCHAR(24) NOT NULL','phase VARCHAR(24)'),
    ('PRIMARY KEY (operation_id, object_index)','PRIMARY KEY (operation_id)'),
    ('ON DELETE RESTRICT','ON DELETE CASCADE'),
    ('ON DELETE RESTRICT','ON DELETE RESTRICT ON UPDATE CASCADE'),
    ("phase IN ('unlink_pending','unlink_confirmed','fsync_confirmed','retired')",'1=1'),
])
def test_unlink_schema_adoption_rejects_weakened_authority(owned_unlink_legacy,old,new):
    module = migration()
    from core.timelapse_unlink_schema import unlinks
    from sqlalchemy.schema import CreateTable
    sql = str(CreateTable(unlinks).compile(dialect=owned_unlink_legacy.dialect))
    assert old in sql
    with owned_unlink_legacy.begin() as db:
        db.exec_driver_sql(sql.replace(old,new))
    with pytest.raises(Exception):
        with owned_unlink_legacy.begin() as db:
            module.apply(db)
    with owned_unlink_legacy.connect() as db:
        assert db.execute(text('SELECT state FROM timelapse_retirement_objects')).scalar_one() == 'planned'


@pytest.mark.parametrize('phase,state',[
    ('unlink_pending','staged'),('unlink_confirmed','unlink_confirmed'),
    ('fsync_confirmed','fsync_confirmed'),('retired','retired'),
])
def test_unlink_recorded_adoption_requires_matching_staged_move_and_object(owned_unlink_legacy,phase,state):
    module = migration()
    with owned_unlink_legacy.begin() as db:
        module.apply(db)
        db.execute(text("UPDATE timelapse_retirement_objects SET state=:state"),{'state':state})
        db.execute(text("INSERT INTO timelapse_retirement_moves(operation_id,object_index,kind,digest_sha256,phase,verified_ctime_ns) VALUES('owned',0,'file',:digest,'staged',100)"),{'digest':'a'*64})
        db.execute(text("INSERT INTO timelapse_retirement_unlinks VALUES('owned',0,:phase)"),{'phase':phase})
        module.apply(db)
        module.validate(db)
        db.execute(text("UPDATE timelapse_retirement_objects SET state='planned'"))
    with pytest.raises(Exception):
        with owned_unlink_legacy.begin() as db:
            module.validate(db)


@pytest.mark.parametrize('move_phase',['missing','planned','rename_pending'])
def test_unlink_adoption_never_infers_staged_source(owned_unlink_legacy,move_phase):
    module = migration()
    with owned_unlink_legacy.begin() as db:
        module.apply(db)
        db.execute(text("UPDATE timelapse_retirement_objects SET state='staged'"))
        if move_phase != 'missing':
            db.execute(text("INSERT INTO timelapse_retirement_moves(operation_id,object_index,kind,digest_sha256,phase) VALUES('owned',0,'file',:digest,:phase)"),{'digest':'a'*64,'phase':move_phase})
        db.execute(text("INSERT INTO timelapse_retirement_unlinks VALUES('owned',0,'unlink_pending')"))
    with pytest.raises(Exception):
        with owned_unlink_legacy.begin() as db:
            module.apply(db)


def test_unlink_adoption_rejects_extra_index(owned_unlink_legacy):
    module = migration()
    with owned_unlink_legacy.begin() as db:
        module.apply(db)
        db.exec_driver_sql('CREATE INDEX unexpected_unlinks ON timelapse_retirement_unlinks(phase)')
    with pytest.raises(Exception):
        with owned_unlink_legacy.begin() as db:
            module.validate(db)
