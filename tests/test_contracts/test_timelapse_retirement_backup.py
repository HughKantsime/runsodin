"""Owned full-database backup retains retirement provenance without touching media."""
import sqlite3
from pathlib import Path
from sqlalchemy import text
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime, review_db
from tests.test_contracts.test_timelapse_retirement_migration import intent_values,object_values

def test_online_backup_retains_intent_manifest_and_live_nonce(review_db,tmp_path):
    from core.timelapse_retirement_schema import intent,objects
    from modules.system.backup_service import create_online_backup,validate_database
    review_db.execute(text("INSERT INTO timelapses(id,printer_id,filename,status,retirement_operation_id) VALUES(7,9,'original.mp4','deleting','owned-a')"))
    review_db.execute(intent.insert(),intent_values())
    review_db.execute(objects.insert(),object_values())
    review_db.commit()
    database=Path(review_db.get_bind().url.database)
    backup,metadata=create_online_backup('sqlite:///'+str(database),base_dir=tmp_path/'owned-backup')
    assert validate_database(backup)['table_count']>0
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT operation_id,timelapse_id,state FROM timelapse_retirements').fetchone()==('owned-a',7,'pending')
        assert db.execute('SELECT object_index,inode,mtime_ns,quarantine_name FROM timelapse_retirement_objects').fetchone()==(0,2**40,1800000000000000000,'owned-a-0')
        assert db.execute('SELECT retirement_operation_id,status FROM timelapses WHERE id=7').fetchone()==('owned-a','deleting')
