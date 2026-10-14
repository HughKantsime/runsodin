"""Publication journal schema cannot infer publisher authority from legacy rows."""
import importlib
import pytest
from sqlalchemy import create_engine,inspect,text

TABLES=('timelapse_media_lifetimes','timelapse_media_versions',
        'timelapse_frame_namespaces','timelapse_frame_bindings',
        'timelapse_publications','timelapse_publication_facts',
        'timelapse_retained_versions')


def migration():
    return importlib.import_module('core.schema.migrations.019_timelapse_publications')


@pytest.fixture
def legacy_publication_database():
    engine=create_engine('sqlite:///:memory:')
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA foreign_keys=ON')
        db.exec_driver_sql('CREATE TABLE timelapses(id INTEGER PRIMARY KEY,printer_id INTEGER,print_job_id INTEGER,filename TEXT,status TEXT)')
        db.exec_driver_sql("INSERT INTO timelapses VALUES(199,9,NULL,'owned-original.mp4','ready')")
        for module in ('015_timelapse_retirements','016_timelapse_ownership','017_timelapse_recovery','018_timelapse_unlinks'):
            importlib.import_module('core.schema.migrations.'+module).apply(db)
    yield engine
    engine.dispose()


def test_publication_empty_repeat_preserves_legacy_without_ownership_inference(legacy_publication_database):
    module=migration()
    with legacy_publication_database.begin() as db:
        module.apply(db);module.apply(db);module.validate(db)
        for table in TABLES:assert db.execute(text('SELECT COUNT(*) FROM '+table)).scalar_one()==0
        assert db.execute(text('SELECT id,filename,status,ownership_operation_id,retirement_operation_id FROM timelapses')).one()==(199,'owned-original.mp4','ready',None,None)
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_ownership_anchors')).scalar_one()==0


def test_publication_caller_rollback_preserves_only_original_schema(legacy_publication_database):
    module=migration()
    with pytest.raises(RuntimeError,match='owned caller rollback'):
        with legacy_publication_database.begin() as db:
            module.apply(db)
            raise RuntimeError('owned caller rollback')
    assert all(not inspect(legacy_publication_database).has_table(table) for table in TABLES)
    with legacy_publication_database.connect() as db:
        assert db.execute(text('SELECT filename,status FROM timelapses')).one()==('owned-original.mp4','ready')


@pytest.mark.parametrize('malformed_table',TABLES)
def test_publication_bad_adoption_refuses_before_creating_other_tables(legacy_publication_database,malformed_table):
    module=migration()
    with legacy_publication_database.begin() as db:db.exec_driver_sql('CREATE TABLE '+malformed_table+'(untrusted TEXT)')
    with pytest.raises(Exception):
        with legacy_publication_database.begin() as db:module.apply(db)
    assert all(not inspect(legacy_publication_database).has_table(table) for table in TABLES if table!=malformed_table)
    with legacy_publication_database.connect() as db:
        assert db.execute(text('SELECT filename,status FROM timelapses')).one()==('owned-original.mp4','ready')


def test_publication_tables_have_explicit_archives_ownership():
    from modules.archives import TABLES as declared
    assert set(TABLES)<=set(declared)


@pytest.fixture
def owned_frame_publication(legacy_publication_database):
    module=migration()
    from core.timelapse_ownership_schema import anchors
    from core.timelapse_publication_schema import lifetimes,versions,namespaces,bindings,publications
    with legacy_publication_database.begin() as db:
        module.apply(db)
        db.execute(anchors.insert(),{'operation_id':'owned','timelapse_id':199,'printer_id':9,'scheduled_job_id':444,'filename':'owned-original.mp4','owner_state':'known','owner_org_id':1,'ownership_source':'first_capture_job_file','print_file_id':111,'capture_printer_org_id':1,'capture_printer_shared':False})
        db.execute(lifetimes.insert(),{'lifetime_nonce':'owned','original_timelapse_id':199,'original_printer_id':9,'root_device':1,'root_inode':100,'root_uid':501,'root_gid':20,'root_mode':16832})
        db.execute(versions.insert(),{'version_nonce':'owned','lifetime_nonce':'owned'})
        db.execute(namespaces.insert(),{'namespace_id':'namespace','lifetime_nonce':'owned','relative_path':'frames_namespace','phase':'bound','device':1,'inode':101,'uid':501,'gid':20,'mode':16832})
        db.execute(bindings.insert(),{'lifetime_nonce':'owned','namespace_id':'namespace','next_number':2})
        db.execute(publications.insert(),{'operation_id':'frame-op','source_nonce':'owned','source_lifetime':'owned','kind':'frame','recipe_version':1,'source_timelapse_id':199,'source_printer_id':9,'source_filename':'owned-original.mp4','source_status':'capturing','frame_namespace_id':'namespace','frame_number':1,'payload_byte_count':1024,'payload_digest_sha256':'a'*64,'private_directory':'publish_frame-op','private_name':'output.jpg','destination_path':'frames_namespace/frame_000001.jpg','result_nonce':'owned','result_lifetime':'owned','result_tid':199,'phase':'admitted','state':'active','revision':1})
    return legacy_publication_database


def test_private_publication_root_refuses_nonprivate_directory_mode(owned_frame_publication):
    from core.timelapse_publication_schema import facts
    with pytest.raises(Exception):
        with owned_frame_publication.begin() as db:
            db.execute(facts.insert(),{'operation_id':'frame-op','role':'private_root','relative_path':'publish_frame-op','kind':'directory','device':1,'inode':102,'uid':501,'gid':20,'mode':16877})


def test_private_publication_root_accepts_exact_owned_0700_shape(owned_frame_publication):
    from core.timelapse_publication_schema import facts
    with owned_frame_publication.begin() as db:
        db.execute(facts.insert(),{'operation_id':'frame-op','role':'private_root','relative_path':'publish_frame-op','kind':'directory','device':1,'inode':102,'uid':501,'gid':20,'mode':16832})
        assert db.execute(facts.select()).mappings().one()['mode']==16832


def test_publication_journal_records_explicit_typed_recipe_columns(legacy_publication_database):
    from core.timelapse_recipes import RECIPE_COLUMNS
    with legacy_publication_database.begin() as db:migration().apply(db)
    actual={column['name'] for column in inspect(legacy_publication_database).get_columns('timelapse_publications')}
    assert set(RECIPE_COLUMNS)<=actual
