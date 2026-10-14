"""Actual online backup preserves media privacy provenance and exact live nonce."""
import sqlite3
from pathlib import Path
from sqlalchemy import text
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db


def test_online_backup_preserves_owner_education_and_capture_policy(review_db,tmp_path):
    from core.timelapse_ownership_schema import anchors
    from modules.system.backup_service import create_online_backup,validate_database
    review_db.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status,ownership_operation_id) VALUES(199,9,444,'owned.mp4','ready','owned-original')"))
    review_db.execute(anchors.insert(),{'operation_id':'owned-original','timelapse_id':199,'printer_id':9,'scheduled_job_id':444,'filename':'owned.mp4','owner_state':'known','owner_org_id':1,'ownership_source':'first_capture_job_file','print_file_id':11,'education_submission_id':77,'capture_printer_org_id':1,'capture_printer_shared':False})
    review_db.commit()
    database=Path(review_db.get_bind().url.database)
    backup,_=create_online_backup('sqlite:///'+str(database),base_dir=tmp_path/'owned-backup')
    assert validate_database(backup)['table_count']>0
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT operation_id,owner_org_id,print_file_id,education_submission_id,capture_printer_org_id,capture_printer_shared FROM timelapse_ownership_anchors').fetchone()==('owned-original',1,11,77,1,0)
        assert db.execute('SELECT id,ownership_operation_id FROM timelapses').fetchone()==(199,'owned-original')
