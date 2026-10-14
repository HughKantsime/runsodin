"""Actual online SQLite backup retains the pending deletion admission receipt."""
import sqlite3
from pathlib import Path
from sqlalchemy import text
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime, review_db
from tests.test_contracts.test_timelapse_retirement_migration import intent_values, object_values


def test_online_backup_preserves_original_facts_and_pending_unlink(review_db,tmp_path):
    from core.timelapse_retirement_schema import intent, objects
    from core.timelapse_recovery_schema import moves
    from core.timelapse_unlink_schema import unlinks
    from modules.system.backup_service import create_online_backup, validate_database
    review_db.execute(intent.insert(),intent_values())
    review_db.execute(objects.insert(),dict(object_values(),state='staged'))
    review_db.execute(moves.insert(),{'operation_id':'owned-a','object_index':0,'kind':'file','digest_sha256':'a'*64,'phase':'staged','verified_ctime_ns':2**55})
    review_db.execute(unlinks.insert(),{'operation_id':'owned-a','object_index':0,'phase':'unlink_pending'})
    review_db.commit()
    original = Path(review_db.get_bind().url.database)
    backup,_ = create_online_backup('sqlite:///'+str(original),base_dir=tmp_path/'owned-backup')
    assert validate_database(backup)['table_count'] > 0
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT operation_id,object_index,phase FROM timelapse_retirement_unlinks').fetchone() == ('owned-a',0,'unlink_pending')
        assert db.execute('SELECT digest_sha256,phase,verified_ctime_ns FROM timelapse_retirement_moves').fetchone() == ('a'*64,'staged',2**55)
        assert db.execute('SELECT state FROM timelapse_retirement_objects').fetchone() == ('staged',)
    assert review_db.execute(text('SELECT phase FROM timelapse_retirement_unlinks')).scalar_one() == 'unlink_pending'
