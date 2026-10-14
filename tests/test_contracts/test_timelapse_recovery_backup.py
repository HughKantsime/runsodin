"""Actual online backup preserves pending recovery proof, without file mutation."""
import sqlite3
from pathlib import Path
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db
from tests.test_contracts.test_timelapse_retirement_migration import intent_values,object_values


def test_online_backup_preserves_parent_quarantine_digest_and_move_phase(review_db,tmp_path):
    from core.timelapse_retirement_schema import intent,objects
    from core.timelapse_recovery_schema import parents,quarantine,moves
    from modules.system.backup_service import create_online_backup,validate_database
    review_db.execute(intent.insert(),intent_values())
    review_db.execute(objects.insert(),object_values())
    directory={'operation_id':'owned-a','device':2**40,'inode':2**45,'uid':501,'gid':20,'mode':16832}
    review_db.execute(parents.insert(),dict(directory,relative_path=''))
    review_db.execute(quarantine.insert(),dict(directory,inode=2**45+1))
    review_db.execute(moves.insert(),{'operation_id':'owned-a','object_index':0,'kind':'file','digest_sha256':'a'*64,'phase':'rename_pending'})
    review_db.commit()
    original=Path(review_db.get_bind().url.database)
    backup,_=create_online_backup('sqlite:///'+str(original),base_dir=tmp_path/'owned-backup')
    assert validate_database(backup)['table_count']>0
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT relative_path,device,inode FROM timelapse_retirement_parents').fetchone()==('',2**40,2**45)
        assert db.execute('SELECT inode,mode FROM timelapse_retirement_quarantine').fetchone()==(2**45+1,16832)
        assert db.execute('SELECT digest_sha256,phase,verified_ctime_ns FROM timelapse_retirement_moves').fetchone()==('a'*64,'rename_pending',None)
