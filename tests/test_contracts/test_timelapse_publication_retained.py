"""Retained journal validation proves relational history, never current file bytes."""
from datetime import datetime,timezone
import pytest
from sqlalchemy import text
from core.schema.migrator import MigrationError
from tests.test_contracts.test_timelapse_publication_migration import legacy_publication_database,owned_frame_publication,migration

DIRECTORY={'device':1,'inode':100,'uid':501,'gid':20,'mode':16832,'kind':'directory'}
EMPTY='e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'


def phase_fixture(engine,phase):
    from core.timelapse_publication_schema import facts,publications
    with engine.begin() as db:
        for role,path,inode in [('root','',100),('source_parent','frames_namespace',101),('output_parent','frames_namespace',101)]:
            db.execute(facts.insert(),dict(DIRECTORY,operation_id='frame-op',role=role,relative_path=path,inode=inode))
        if phase!='admitted':
            db.execute(facts.insert(),dict(DIRECTORY,operation_id='frame-op',role='private_root',relative_path='publish_frame-op',inode=102))
            db.execute(facts.insert(),{'operation_id':'frame-op','role':'private_creation','relative_path':'output.jpg','kind':'file','device':1,'inode':103,'uid':501,'gid':20,'mode':33152,'link_count':1,'byte_count':0,'mtime_ns':100,'ctime_ns':100,'digest_sha256':EMPTY})
        if phase in ('output_ready','publish_pending','published','complete'):
            db.execute(facts.insert(),{'operation_id':'frame-op','role':'output','relative_path':'output.jpg','kind':'file','device':1,'inode':103,'uid':501,'gid':20,'mode':33152,'link_count':1,'byte_count':1024,'mtime_ns':200,'ctime_ns':200,'digest_sha256':'a'*64})
        change={'phase':phase}
        if phase in ('published','complete'):change['published_ctime_ns']=300
        if phase=='complete':change.update(state='complete',completed_at=datetime.now(timezone.utc))
        db.execute(publications.update().values(**change))


@pytest.mark.parametrize('phase',['admitted','private_created','output_ready','publish_pending','published','complete'])
def test_valid_retained_frame_phase_can_be_validated_and_readopted(owned_frame_publication,phase):
    phase_fixture(owned_frame_publication,phase)
    with owned_frame_publication.begin() as db:
        migration().validate(db);migration().apply(db)
        assert db.execute(text('SELECT phase FROM timelapse_publications')).scalar_one()==phase


@pytest.mark.parametrize('damage',['missing_root','missing_output_parent','missing_private_root','missing_creation','missing_output','wrong_root_owner','wrong_namespace_owner','counter_float','counter_reused','private_output_replacement','frame_payload_digest','unplanned_output','namespace_path','destination_path'])
def test_inconsistent_retained_history_refuses_without_repair(owned_frame_publication,damage):
    phase_fixture(owned_frame_publication,'output_ready')
    with owned_frame_publication.begin() as db:
        if damage.startswith('missing_'):
            role={'missing_root':'root','missing_output_parent':'output_parent','missing_private_root':'private_root','missing_creation':'private_creation','missing_output':'output'}[damage]
            db.execute(text('DELETE FROM timelapse_publication_facts WHERE role=:role'),{'role':role})
        elif damage=='wrong_root_owner':db.execute(text("UPDATE timelapse_publication_facts SET uid=uid+1 WHERE role='root'"))
        elif damage=='wrong_namespace_owner':db.execute(text('UPDATE timelapse_frame_namespaces SET uid=uid+1'))
        elif damage=='counter_float':db.execute(text('UPDATE timelapse_frame_bindings SET next_number=1.5'))
        elif damage=='counter_reused':db.execute(text('UPDATE timelapse_frame_bindings SET next_number=1'))
        elif damage=='private_output_replacement':db.execute(text("UPDATE timelapse_publication_facts SET inode=inode+1 WHERE role='output'"))
        elif damage=='frame_payload_digest':db.execute(text("UPDATE timelapse_publication_facts SET digest_sha256=:digest WHERE role='output'"),{'digest':'b'*64})
        elif damage=='unplanned_output':db.execute(text("UPDATE timelapse_publication_facts SET relative_path='unrelated.jpg' WHERE role='output'"))
        elif damage=='namespace_path':db.execute(text("UPDATE timelapse_frame_namespaces SET relative_path='old-id-only'"))
        else:db.execute(text("UPDATE timelapse_publications SET destination_path='../outside.jpg'"))
    with pytest.raises(MigrationError,match="Publication retained authority differs"):
        with owned_frame_publication.begin() as db:migration().validate(db)
    with owned_frame_publication.connect() as db:
        assert db.execute(text('SELECT phase FROM timelapse_publications')).scalar_one()=='output_ready'
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_publications')).scalar_one()==1


@pytest.mark.parametrize('kind',['encode','trim','speed'])
def test_admitted_nonframe_operation_has_exact_result_lifetime_and_manifest(owned_frame_publication,kind):
    from core.timelapse_ownership_schema import anchors
    from core.timelapse_publication_schema import lifetimes,versions,publications,facts
    phase_fixture(owned_frame_publication,'complete')
    with owned_frame_publication.begin() as db:
        original=dict(db.execute(anchors.select()).mappings().one())
        result_nonce=kind+'-result';target=kind+'.mp4';result_tid=299 if kind=='speed' else 199
        new_owner=dict(original,operation_id=result_nonce,timelapse_id=result_tid,filename=target);db.execute(anchors.insert(),new_owner)
        result_life=result_nonce if kind=='speed' else 'owned'
        if kind=='speed':db.execute(lifetimes.insert(),{'lifetime_nonce':result_life,'original_timelapse_id':299,'original_printer_id':9,'root_device':1,'root_inode':100,'root_uid':501,'root_gid':20,'root_mode':16832})
        db.execute(versions.insert(),{'version_nonce':result_nonce,'lifetime_nonce':result_life})
        op={'operation_id':kind+'-op','source_nonce':'owned','source_lifetime':'owned','kind':kind,'source_timelapse_id':199,'source_printer_id':9,'source_filename':'owned-original.mp4','source_status':'encoding' if kind=='encode' else 'ready','source_preclaim_status':'capturing' if kind=='encode' else None,'actor_id':None if kind=='encode' else 3,'private_directory':'publish_'+kind+'-op','private_name':'output.mp4','destination_path':target,'result_nonce':result_nonce,'result_lifetime':result_life,'result_tid':result_tid,'phase':'admitted','state':'active','revision':1}
        from core.timelapse_recipes import make_recipe
        args={'trim':{'start_seconds':0,'end_seconds':1},'speed':{'multiplier':1}}.get(kind,{})
        op.update(make_recipe(kind,**args).columns())
        db.execute(publications.insert(),op)
        for role in ('root','output_parent'):db.execute(facts.insert(),dict(DIRECTORY,operation_id=op['operation_id'],role=role,relative_path=''))
        if kind=='encode':
            db.execute(facts.insert(),dict(DIRECTORY,operation_id=op['operation_id'],role='source_parent',relative_path='frames_namespace',inode=101))
            source={'relative_path':'frames_namespace/frame_000001.jpg','inode':103,'byte_count':1024,'mtime_ns':200,'ctime_ns':300,'digest_sha256':'a'*64}
        else:source={'relative_path':'owned-original.mp4','inode':200,'byte_count':2048,'mtime_ns':300,'ctime_ns':300,'digest_sha256':'b'*64}
        db.execute(facts.insert(),dict(source,operation_id=op['operation_id'],role='source',kind='file',device=1,uid=501,gid=20,mode=33152,link_count=1))
        migration().validate(db)


def test_lifetime_without_initial_version_mapping_refuses(owned_frame_publication):
    with owned_frame_publication.begin() as db:
        db.execute(text('DELETE FROM timelapse_publications'))
        db.execute(text('DELETE FROM timelapse_media_versions'))
    with pytest.raises(MigrationError,match="Publication retained authority differs"):
        with owned_frame_publication.begin() as db:migration().validate(db)


def test_nested_output_requires_every_parent_identity(owned_frame_publication):
    from core.timelapse_publication_schema import facts,publications
    # Frame paths are fixed; demonstrate complete parent coverage directly
    # through the production validator's non-frame branch below.
    from core.timelapse_ownership_schema import anchors
    from core.timelapse_publication_schema import versions
    phase_fixture(owned_frame_publication,'complete')
    with owned_frame_publication.begin() as db:
        original=dict(db.execute(anchors.select()).mappings().one())
        db.execute(anchors.insert(),dict(original,operation_id='trim-result',filename='folder/nested/trim.mp4'))
        db.execute(versions.insert(),{'version_nonce':'trim-result','lifetime_nonce':'owned'})
        db.execute(publications.insert(),{'operation_id':'trim-op','source_nonce':'owned','source_lifetime':'owned','kind':'trim','recipe_version':1,'trim_start':'0','trim_end':'1','source_timelapse_id':199,'source_printer_id':9,'source_filename':'owned-original.mp4','source_status':'ready','actor_id':3,'private_directory':'publish_trim-op','private_name':'output.mp4','destination_path':'folder/nested/trim.mp4','result_nonce':'trim-result','result_lifetime':'owned','result_tid':199,'phase':'admitted','state':'active','revision':1})
        db.execute(facts.insert(),dict(DIRECTORY,operation_id='trim-op',role='root',relative_path=''))
        db.execute(facts.insert(),dict(DIRECTORY,operation_id='trim-op',role='output_parent',relative_path='folder/nested',inode=901))
        db.execute(facts.insert(),{'operation_id':'trim-op','role':'source','relative_path':'owned-original.mp4','kind':'file','device':1,'inode':200,'uid':501,'gid':20,'mode':33152,'link_count':1,'byte_count':2048,'mtime_ns':300,'ctime_ns':300,'digest_sha256':'b'*64})
    with pytest.raises(MigrationError,match="Publication retained authority differs"):
        with owned_frame_publication.begin() as db:migration().validate(db)
    with owned_frame_publication.begin() as db:
        db.execute(facts.insert(),dict(DIRECTORY,operation_id='trim-op',role='output_parent',relative_path='folder',inode=900))
        migration().validate(db)


def completed_trim_retirement(engine):
    """Exact retained-source journal, without pretending to check disk bytes."""
    from core.timelapse_ownership_schema import anchors
    from core.timelapse_publication_schema import versions,publications,facts,retained
    from core.timelapse_retirement_schema import intent,objects
    from core.timelapse_recovery_schema import moves
    from core.timelapse_unlink_schema import unlinks
    phase_fixture(engine,'complete')
    with engine.begin() as db:
        original=dict(db.execute(anchors.select()).mappings().one())
        db.execute(anchors.insert(),dict(original,operation_id='trim-result',filename='trim.mp4'))
        db.execute(versions.insert(),{'version_nonce':'trim-result','lifetime_nonce':'owned'})
        db.execute(publications.insert(),{'operation_id':'trim-op','source_nonce':'owned','source_lifetime':'owned','kind':'trim','recipe_version':1,'trim_start':'0','trim_end':'1','source_timelapse_id':199,'source_printer_id':9,'source_filename':'owned-original.mp4','source_status':'ready','actor_id':3,'private_directory':'publish_trim-op','private_name':'output.mp4','destination_path':'trim.mp4','result_nonce':'trim-result','result_lifetime':'owned','result_tid':199,'phase':'complete','state':'complete','revision':1,'published_ctime_ns':700,'completed_at':datetime.now(timezone.utc)})
        for role,path,inode in [('root','',100),('output_parent','',100),('private_root','publish_trim-op',201)]:
            db.execute(facts.insert(),dict(DIRECTORY,operation_id='trim-op',role=role,relative_path=path,inode=inode))
        source={'device':1,'inode':200,'uid':501,'gid':20,'mode':33152,'link_count':1,'byte_count':2048,'mtime_ns':300,'ctime_ns':300}
        db.execute(facts.insert(),dict(source,operation_id='trim-op',role='source',relative_path='owned-original.mp4',kind='file',digest_sha256='b'*64))
        for role,size,time,digest in [('private_creation',0,400,EMPTY),('output',1024,600,'c'*64)]:
            db.execute(facts.insert(),dict(source,operation_id='trim-op',role=role,relative_path='output.mp4',kind='file',inode=202,byte_count=size,mtime_ns=time,ctime_ns=time,digest_sha256=digest))
        db.execute(intent.insert(),{'operation_id':'trim-result','timelapse_id':199,'printer_id':9,'owner_state':'known','owner_org_id':1,'filename':'trim.mp4','root_device':1,'root_inode':100,'state':'complete','revision':1,'completed_at':datetime.now(timezone.utc)})
        db.execute(objects.insert(),dict(source,operation_id='trim-result',object_index=0,relative_path='owned-original.mp4',kind='file',state='retired',quarantine_name='old-media'))
        db.execute(moves.insert(),{'operation_id':'trim-result','object_index':0,'kind':'file','digest_sha256':'b'*64,'phase':'staged','verified_ctime_ns':800})
        db.execute(unlinks.insert(),{'operation_id':'trim-result','object_index':0,'phase':'retired'})
        db.execute(retained.insert(),{'version_nonce':'owned','lifetime_nonce':'owned','retained_by_operation':'trim-op','filename':'owned-original.mp4','state':'retired','retire_operation_id':'trim-result'})


def test_retained_retirement_requires_exact_source_and_durable_receipts(owned_frame_publication):
    completed_trim_retirement(owned_frame_publication)
    with owned_frame_publication.begin() as db:migration().validate(db)


@pytest.mark.parametrize('damage',['missing_move','missing_unlink','wrong_digest','wrong_inode','wrong_mtime'])
def test_retired_reference_cannot_use_unproved_file_receipts(owned_frame_publication,damage):
    from core.timelapse_publication_validation import validate_retained
    from core.schema.migrator import MigrationError
    completed_trim_retirement(owned_frame_publication)
    with owned_frame_publication.begin() as db:
        if damage=='missing_move':
            db.execute(text('DELETE FROM timelapse_retirement_unlinks'))
            db.execute(text('DELETE FROM timelapse_retirement_moves'))
        elif damage=='missing_unlink':db.execute(text('DELETE FROM timelapse_retirement_unlinks'))
        elif damage=='wrong_digest':db.execute(text('UPDATE timelapse_retirement_moves SET digest_sha256=:digest'),{'digest':'d'*64})
        elif damage=='wrong_inode':db.execute(text('UPDATE timelapse_retirement_objects SET inode=inode+1'))
        else:db.execute(text('UPDATE timelapse_retirement_objects SET mtime_ns=mtime_ns+1'))
        with pytest.raises(MigrationError,match="Publication retained authority differs"):validate_retained(db)


@pytest.mark.parametrize('existing_deferred',[False,True])
def test_sqlite_adoption_excludes_concurrent_publisher(owned_frame_publication,tmp_path,existing_deferred):
    """Actual second connection, including an already-started DEFERRED caller."""
    import sqlite3
    from sqlalchemy import create_engine
    phase_fixture(owned_frame_publication,'admitted')
    path=tmp_path/'adoption.sqlite'
    with owned_frame_publication.connect() as source:
        with sqlite3.connect(path) as target:
            source.connection.driver_connection.backup(target)
    candidate=create_engine('sqlite:///'+str(path))
    writer=sqlite3.connect(path,timeout=0)
    try:
        with candidate.begin() as db:
            db.exec_driver_sql('PRAGMA foreign_keys=ON')
            if existing_deferred:db.exec_driver_sql('BEGIN')
            migration().validate(db)
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                writer.execute('BEGIN IMMEDIATE')
            # Adoption does not commit or change the caller's protected data.
            assert db.connection.driver_connection.in_transaction
            assert db.connection.driver_connection.total_changes == 0
        writer.execute('BEGIN IMMEDIATE')
        writer.rollback()
    finally:
        writer.close();candidate.dispose()


@pytest.mark.parametrize('entrypoint',['validate','apply'])
def test_sqlite_publisher_first_refuses_adoption_without_owning_rollback(owned_frame_publication,tmp_path,entrypoint):
    import sqlite3
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError
    phase_fixture(owned_frame_publication,'admitted')
    path=tmp_path/'publisher-first.sqlite'
    with owned_frame_publication.connect() as source:
        with sqlite3.connect(path) as target:
            source.connection.driver_connection.backup(target)
    candidate=create_engine('sqlite:///'+str(path),connect_args={'timeout':0})
    writer=sqlite3.connect(path,timeout=0)
    try:
        writer.execute('BEGIN IMMEDIATE')
        with candidate.connect() as db:
            db.exec_driver_sql('BEGIN')
            with pytest.raises(OperationalError,match='locked'):
                getattr(migration(),entrypoint)(db)
            assert db.connection.driver_connection.in_transaction
            assert db.connection.driver_connection.total_changes == 0
            db.rollback()
        assert writer.in_transaction
        writer.rollback()
        with candidate.begin() as db:
            db.exec_driver_sql('PRAGMA foreign_keys=ON')
            migration().validate(db)
    finally:
        writer.close();candidate.dispose()


def test_sqlite_stale_snapshot_is_not_silently_retried(owned_frame_publication,tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError
    phase_fixture(owned_frame_publication,'admitted')
    path=tmp_path/'stale-snapshot.sqlite'
    with owned_frame_publication.connect() as source:
        with sqlite3.connect(path) as target:
            source.connection.driver_connection.backup(target)
    writer=sqlite3.connect(path,timeout=0)
    assert writer.execute('PRAGMA journal_mode=WAL').fetchone()[0]=='wal'
    candidate=create_engine('sqlite:///'+str(path),connect_args={'timeout':0})
    try:
        with candidate.connect() as db:
            db.exec_driver_sql('BEGIN')
            assert db.exec_driver_sql('SELECT next_number FROM timelapse_frame_bindings').scalar_one()==2
            writer.execute('UPDATE timelapse_frame_bindings SET next_number=3')
            writer.commit()
            with pytest.raises(OperationalError,match='locked'):migration().validate(db)
            assert db.connection.driver_connection.in_transaction
            assert db.connection.driver_connection.total_changes==0
            assert db.exec_driver_sql('SELECT next_number FROM timelapse_frame_bindings').scalar_one()==2
            db.rollback()
            assert db.exec_driver_sql('SELECT next_number FROM timelapse_frame_bindings').scalar_one()==3
    finally:
        writer.close();candidate.dispose()


def test_online_backup_restores_retained_publication_and_retirement_authority(owned_frame_publication,tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    from core.timelapse_publication_schema import TABLES
    completed_trim_retirement(owned_frame_publication)
    path=tmp_path/'publication-backup.sqlite'
    with owned_frame_publication.connect() as source:
        expected={table.name:source.execute(text('SELECT COUNT(*) FROM '+table.name)).scalar_one() for table in TABLES}
        with sqlite3.connect(path) as target:
            source.connection.driver_connection.backup(target)
    restored=create_engine('sqlite:///'+str(path))
    try:
        with restored.begin() as db:
            db.exec_driver_sql('PRAGMA foreign_keys=ON')
            migration().validate(db);migration().apply(db)
            assert {table.name:db.execute(text('SELECT COUNT(*) FROM '+table.name)).scalar_one() for table in TABLES}==expected
            assert db.execute(text('SELECT state,retire_operation_id FROM timelapse_retained_versions')).one()==('retired','trim-result')
            assert db.execute(text('SELECT phase FROM timelapse_retirement_unlinks')).scalar_one()=='retired'
            assert db.exec_driver_sql('PRAGMA foreign_key_check').first() is None
    finally:restored.dispose()


@pytest.mark.parametrize('kind,field,value',[
    ('frame','recipe_version',None),('frame','recipe_version',2),
    ('frame','encode_fps',30),('frame','trim_start','0'),
    ('encode','encode_fps',None),('encode','encode_fps',31),
    ('speed','speed_denominator',None),('speed','speed_numerator',7),
    ('trim','trim_start',None),
])
def test_recipe_sql_shape_refuses_missing_or_wrong_kind_settings(owned_frame_publication,kind,field,value):
    from sqlalchemy.exc import IntegrityError
    from core.timelapse_publication_schema import publications
    if kind=='frame':phase_fixture(owned_frame_publication,'admitted')
    else:test_admitted_nonframe_operation_has_exact_result_lifetime_and_manifest(owned_frame_publication,kind)
    with pytest.raises(IntegrityError):
        with owned_frame_publication.begin() as db:
            db.execute(publications.update().where(publications.c.kind==kind).values(**{field:value}))
    with owned_frame_publication.begin() as db:migration().validate(db)


@pytest.mark.parametrize('field,value',[
    ('trim_start','1e-6'),('trim_start','0.0'),('trim_start','-0'),
    ('trim_end','9223372036854.775808'),('trim_end','0'),
])
def test_noncanonical_or_unordered_saved_trim_refuses_adoption_without_repair(owned_frame_publication,field,value):
    from core.timelapse_publication_schema import publications
    test_admitted_nonframe_operation_has_exact_result_lifetime_and_manifest(owned_frame_publication,'trim')
    with owned_frame_publication.begin() as db:
        db.execute(publications.update().where(publications.c.kind=='trim').values(**{field:value}))
    with pytest.raises(MigrationError,match='Publication retained authority differs'):
        with owned_frame_publication.begin() as db:migration().apply(db)
    with owned_frame_publication.connect() as db:
        assert db.execute(publications.select().where(publications.c.kind=='trim')).mappings().one()[field]==value


def test_recipe_helper_source_pin_is_required_before_adoption(owned_frame_publication,monkeypatch):
    monkeypatch.setattr(migration(),'RECIPE_SOURCE_SHA256','0'*64)
    with pytest.raises(MigrationError,match='Publication recipe source contract changed'):
        with owned_frame_publication.begin() as db:migration().validate(db)
