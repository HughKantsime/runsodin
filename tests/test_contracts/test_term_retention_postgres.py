"""Owned native PostgreSQL proof for migration 055 and row-locked runtime cleanup."""
import importlib
import hashlib
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from tests.test_contracts.test_post027_migration_033_postgres import pg_engine
from modules.organizations.education_term_retention import TermPolicy, create_policy, preview
from modules.organizations.education_term_cleanup import purge

M = importlib.import_module('core.schema.migrations.055_term_retention')


def test_055_upgrade_and_runtime_on_postgres(pg_engine, tmp_path, monkeypatch):
    from core.schema import bootstrap as boot
    original = boot.POST027_MIGRATIONS
    monkeypatch.setattr(boot, 'POST027_MIGRATIONS', original[:-1])
    monkeypatch.setattr(boot, 'RAW_REQUIRED_TABLES', boot.RAW_REQUIRED_TABLES - set(M.COLUMNS))
    monkeypatch.setattr(boot, 'RAW_REQUIRED_COLUMNS', {k:v for k,v in boot.RAW_REQUIRED_COLUMNS.items() if k not in M.COLUMNS})
    bootstrap_database(pg_engine)
    monkeypatch.undo()
    assert bootstrap_database(pg_engine)['applied'] == [M.MIGRATION_ID]
    assert bootstrap_database(pg_engine)['applied'] == []
    root = tmp_path / 'uploads'
    (root / '1').mkdir(parents=True)
    payload = root / '1' / 'owned.3mf'
    payload.write_bytes(b'owned PG upload')
    monkeypatch.setenv('EDUCATION_UPLOAD_ROOT', str(root))
    with pg_engine.begin() as c:
        M.validate(c)
        for statement in (
            "INSERT INTO groups(id,name,is_org) VALUES(1,'school',TRUE)",
            "INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'student','fixture','viewer',FALSE,1),(3,'admin','fixture','admin',TRUE,1)",
            "INSERT INTO education_cost_centers(id,org_id,name_key,display_name,code_key,code,created_by) VALUES(7,1,'c','C','c','C',3)",
            "INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,accounted_bytes,final_path,expected_hash) VALUES('owned',1,1,'committed',100,:path,:hash)",
            "INSERT INTO print_files(id,filename,org_id,created_by,stored_path,storage_bytes,blob_state) VALUES(11,'owned.3mf',1,1,:path,100,'present')",
            "INSERT INTO models(id,name,print_file_id,org_id) VALUES(12,'owned',11,1)",
            "INSERT INTO jobs(id,item_name,model_id,status,submitted_by,charged_to_user_id,charged_to_org_id) VALUES(13,'owned',12,'completed',1,1,1)",
            "INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status,lifecycle_revision,created_at) VALUES(14,1,'owned',13,11,12,7,1,'completed',1,'2020-01-15')",
            "INSERT INTO education_storage_accounts(org_id,scope_kind,scope_id,user_id,accounted_bytes) VALUES(1,'user',1,1,100),(1,'tenant',1,NULL,100)",
        ):
            c.execute(text(statement), {'path': str(payload), 'hash': hashlib.sha256(payload.read_bytes()).hexdigest()})
    with Session(pg_engine) as db:
        principal = {'id': 3, 'role': 'admin', 'group_id': 1}
        term = create_policy(db, TermPolicy(cost_center_id=7, name='owned', starts_at='2020-01-01T00:00:00Z', ends_at='2020-02-01T00:00:00Z', files_days=0, thumbnails_days=0, portfolio_days=0), principal)
        result = purge(db, term['id'], preview(db, term)['fingerprint'], principal)
        assert len(result['results']) == 3 and {r['state'] for r in result['results']} == {'done'}
        assert not payload.exists()
        assert db.execute(text('SELECT SUM(accounted_bytes) FROM education_storage_accounts')).scalar() == 0
        assert db.execute(text("SELECT count(*) FROM education_audit_events WHERE action LIKE 'term_retention_%'")).scalar() == 3
        assert purge(db, term['id'], preview(db, term)['fingerprint'], principal)['results'] == []
        # A competing activation must be fenced before each eligibility read,
        # both during selection validation and immediately before erasure.
        from sqlalchemy import event
        from sqlalchemy.exc import OperationalError
        from fastapi import HTTPException
        from modules.organizations.education_term_privacy import anonymize
        fingerprint = preview(db, term)['fingerprint']
        db.rollback()
        with pg_engine.connect() as holder:
            holder.execute(text('SELECT id FROM users WHERE id=1 FOR UPDATE'))
            with pytest.raises(HTTPException) as caught:
                anonymize(db, term['id'], [1], fingerprint, principal)
            assert caught.value.status_code == 409
            assert caught.value.detail == 'student_busy_retry'
            assert not db.in_transaction()
            holder.rollback()
        checked = []
        def verify_student_lock(conn, cursor, statement, params, context, executemany):
            if statement.startswith('SELECT role,is_active,group_id,username FROM users'):
                with pg_engine.connect() as contender:
                    contender.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    with pytest.raises(OperationalError):
                        contender.execute(text('UPDATE users SET is_active=TRUE WHERE id=1'))
                    contender.rollback()
                checked.append(statement)
        event.listen(pg_engine, 'after_cursor_execute', verify_student_lock)
        try:
            assert anonymize(db, term['id'], [1], preview(db, term)['fingerprint'], principal)['results'] == [
                {'user_id': 1, 'state': 'anonymized'}]
            assert len(checked) == 2
        finally:
            event.remove(pg_engine, 'after_cursor_execute', verify_student_lock)
