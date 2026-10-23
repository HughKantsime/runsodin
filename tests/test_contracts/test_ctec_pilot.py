"""Offline acceptance of pilot archives, additive schema and stale repairs."""
import importlib
import sqlite3
from uuid import uuid4
import pytest
from sqlalchemy import text, inspect
from fastapi import HTTPException
from core.errors import OdinError
from tests.test_contracts.test_spool_slot_prevention import graph
from tests.test_contracts.test_education_review_workflow import review_db
from modules.organizations.routes_education_reviews import SubmissionArchive
from modules.organizations.education_submission_archive import archive_submission
from modules.organizations.education_review_service import list_visible_submissions

ADMIN = {'id': 3, 'role': 'admin', 'group_id': 1}


@pytest.mark.parametrize('actor', [ADMIN, {'id': 2, 'role': 'viewer', 'group_id': 1}])
def test_archive_preserves_terminal_history_and_audits(review_db, actor):
    db = review_db
    row = db.execute(text('SELECT id,job_id,lifecycle_revision FROM education_submissions LIMIT 1')).first()
    db.execute(text("UPDATE education_submissions SET status='rejected' WHERE id=:id"), {'id': row.id})
    db.execute(text("UPDATE jobs SET status='rejected',rejected_reason='Keep this reason' WHERE id=:id"), {'id': row.job_id})
    db.commit()
    body = SubmissionArchive(revision=row.lifecycle_revision, command_id=uuid4())
    result = archive_submission(db, submission_id=row.id, body=body, principal=actor)
    assert result['status'] == 'rejected' and result['archived']
    assert not list_visible_submissions(db, principal=actor)['items']
    assert list_visible_submissions(db, principal=actor, archived=True)['items'][0]['status'] == 'rejected'
    assert db.execute(text('SELECT rejected_reason FROM jobs WHERE id=:id'), {'id': row.job_id}).scalar() == 'Keep this reason'
    assert db.execute(text("SELECT COUNT(*) FROM education_audit_events WHERE action='submission.archive'")).scalar() == 1
    assert archive_submission(db, submission_id=row.id, body=body, principal=actor) == result


def test_archive_refuses_live_work(review_db):
    row = review_db.execute(text('SELECT id,lifecycle_revision FROM education_submissions LIMIT 1')).first()
    with pytest.raises(OdinError):
        archive_submission(review_db, submission_id=row.id, body=SubmissionArchive(revision=row.lifecycle_revision, command_id=uuid4()), principal=ADMIN)


def test_migration_rerun_and_deferred_unique_index(graph):
    migration = importlib.import_module('core.schema.migrations.036_ctec_pilot')
    graph.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE printer_id=10')); graph.commit()
    with graph.get_bind().begin() as conn:
        migration.apply(conn)
        assert not migration.retry_spool_index(conn)
        conn.execute(text('UPDATE filament_slots SET assigned_spool_id=NULL WHERE printer_id=10'))
        assert migration.retry_spool_index(conn)
        migration.apply(conn)
        assert any(i['name'] == 'uq_filament_slots_spool' for i in inspect(conn).get_indexes('filament_slots'))


@pytest.mark.parametrize("unplug_ht", [False, True])
def test_stale_unlinked_location_self_heals_once_with_audit(graph, unplug_ht):
    from modules.printers.monitors.ams_inventory import sync_ams_inventory
    graph.execute(text('UPDATE filament_slots SET assigned_spool_id=NULL WHERE printer_id=9'))
    if unplug_ht:
        graph.execute(text("INSERT INTO spools(id,filament_id,org_id,location_printer_id,location_slot) VALUES(200,199,1,9,17)"))
        graph.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,assigned_spool_id) VALUES(9,17,'PLA',200)"))
    graph.commit()
    conn = sqlite3.connect(graph.get_bind().url.database)
    report = [{'id': '0', 'tray': [{'id': '0', 'tray_type': 'PLA', 'tag_uid': '0000000000000000'}]}]
    try:
        sync_ams_inventory(conn, 9, report); conn.commit()
        assert conn.execute('SELECT location_printer_id,location_slot FROM spools WHERE id=199').fetchone() == (None, None)
        assert conn.execute('SELECT COUNT(*) FROM inventory_repairs').fetchone() == (1,)
        if unplug_ht:
            assert conn.execute('SELECT assigned_spool_id,filament_type FROM filament_slots WHERE printer_id=9 AND slot_number=17').fetchone() == (None, 'empty')
            assert conn.execute('SELECT location_printer_id,location_slot FROM spools WHERE id=200').fetchone() == (None, None)
        sync_ams_inventory(conn, 9, report); conn.commit()
        assert conn.execute('SELECT COUNT(*) FROM inventory_repairs').fetchone() == (1,)
    finally:
        conn.close()


def test_model_delete_returns_truthful_conflict(review_db):
    from modules.models_library.education_retention import assert_model_deletable
    model = review_db.execute(text('SELECT model_id FROM education_submissions LIMIT 1')).scalar()
    with pytest.raises(HTTPException) as error:
        assert_model_deletable(review_db, model)
    assert error.value.status_code == 409 and 'hide it instead' in error.value.detail


@pytest.mark.parametrize('principal', [
    {'id': 1, 'role': 'viewer', 'group_id': 1},
    {'id': 5, 'role': 'admin', 'group_id': 2},
])
def test_archive_denies_student_and_other_tenant(review_db, principal):
    row = review_db.execute(text('SELECT id,lifecycle_revision FROM education_submissions LIMIT 1')).first()
    with pytest.raises(OdinError):
        archive_submission(review_db, submission_id=row.id,
            body=SubmissionArchive(revision=row.lifecycle_revision, command_id=uuid4()), principal=principal)
    assert review_db.execute(text('SELECT archived_at FROM education_submissions WHERE id=:id'), {'id': row.id}).scalar() is None


def test_archive_refuses_stale_revision_and_unresolved_monitor_claim(review_db):
    db = review_db
    row = db.execute(text('SELECT id,job_id,lifecycle_revision FROM education_submissions LIMIT 1')).first()
    db.execute(text("UPDATE education_submissions SET status='failed' WHERE id=:id"), {'id': row.id})
    db.execute(text("UPDATE jobs SET status='failed' WHERE id=:id"), {'id': row.job_id})
    db.execute(text("INSERT INTO education_monitor_claims(claim_id,org_id,submission_id,job_id,printer_id,authority_revision,token_digest,state) VALUES(:claim,1,:submission,:job,9,1,:token,'awaiting')"),
               {'claim': str(uuid4()), 'submission': row.id, 'job': row.job_id, 'token': 'f' * 64})
    db.commit()
    for revision in [row.lifecycle_revision + 1, row.lifecycle_revision]:
        with pytest.raises(OdinError):
            archive_submission(db, submission_id=row.id, body=SubmissionArchive(revision=revision, command_id=uuid4()), principal=ADMIN)
    assert db.execute(text('SELECT archived_at FROM education_submissions WHERE id=:id'), {'id': row.id}).scalar() is None


def test_sqlite_backup_restore_retains_archive_and_immutable_audit(review_db, tmp_path):
    migration = importlib.import_module('core.schema.migrations.036_ctec_pilot')
    source = sqlite3.connect(review_db.get_bind().url.database)
    destination = sqlite3.connect(tmp_path / 'restored.db')
    try:
        source.backup(destination)
        assert destination.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert {'archived_at', 'archived_by', 'archive_reason'} <= {r[1] for r in destination.execute('PRAGMA table_info(education_submissions)')}
        assert destination.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='education_audit_events'").fetchone()[0] >= 2
    finally:
        destination.close(); source.close()


def test_readiness_separates_signin_from_auto_create(review_db):
    from modules.organizations.education_readiness import readiness_for
    db = review_db
    db.execute(text("INSERT INTO oidc_config(id,is_enabled,provider_type,client_id,client_secret_encrypted,auto_create_users,default_group_id) VALUES(7,TRUE,'google','test-client','synthetic-test-only',FALSE,NULL)"))
    db.commit()
    config = readiness_for(db, 1)['oidc']
    assert config['ready'] and config['tenant_ready'] and not config['reasons']
    db.execute(text('UPDATE oidc_config SET auto_create_users=TRUE WHERE id=7')); db.commit()
    config = readiness_for(db, 1)['oidc']
    assert config['ready'] and not config['tenant_ready'] and config['tenant_reasons']


def test_superadmin_grant_is_refused_with_school_account_guidance(review_db):
    import asyncio
    from modules.organizations.routes_education import replace_cost_center_grants
    from modules.organizations.education_schemas import GrantReplacement, GrantInput
    db = review_db
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(88,'platform-admin','fixture','admin',TRUE,NULL)")); db.commit()
    center = db.execute(text('SELECT id,revision FROM education_cost_centers WHERE org_id=1 LIMIT 1')).first()
    body = GrantReplacement(revision=center.revision, org_id=1, command_id=uuid4(), grants=[GrantInput(user_id=88, roles=['manager'])])
    with pytest.raises(OdinError) as error:
        asyncio.run(replace_cost_center_grants(center.id, body, {'id': 88, 'role': 'admin', 'group_id': None}, db))
    assert 'use a school-level admin account' in str(error.value)
    assert db.execute(text('SELECT group_id FROM users WHERE id=88')).scalar() is None


def test_explicit_inventory_resolution_is_audited_and_tenant_bound(graph):
    from modules.inventory.routes.conflicts import resolve, conflicts, Resolution
    db = graph
    db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE printer_id=10')); db.commit()
    target = db.execute(text('SELECT id FROM filament_slots WHERE printer_id=10')).scalar()
    report = conflicts(ADMIN, db)
    assert report['duplicates'][0]['spool_id'] == 199
    assert resolve(Resolution(spool_id=199, slot_id=target), ADMIN, db) == {'resolved': True}
    assert not conflicts(ADMIN, db)['duplicates']
    assert db.execute(text("SELECT COUNT(*) FROM inventory_repairs WHERE action='admin.resolve'")).scalar() == 1
    with pytest.raises(HTTPException) as error:
        resolve(Resolution(spool_id=199), {'id': 5, 'role': 'admin', 'group_id': 2}, db)
    assert error.value.status_code == 404


@pytest.mark.parametrize("units, assigned, location", [
    ([], None, None),
    ([{"id": "128", "tray": [{"id": "0", "tray_color": "123ABCFF"}]}], 199, 17),
])
def test_ht_removal_clears_but_partial_material_delta_preserves_assignment(graph, units, assigned, location):
    from modules.printers.monitors.ams_inventory import sync_ams_inventory
    graph.execute(text('UPDATE filament_slots SET slot_number=17 WHERE printer_id=9'))
    graph.execute(text('UPDATE spools SET location_slot=17 WHERE id=199')); graph.commit()
    conn = sqlite3.connect(graph.get_bind().url.database)
    try:
        assert sync_ams_inventory(conn, 9, units) == 0
        conn.commit()
        assert conn.execute('SELECT assigned_spool_id FROM filament_slots WHERE printer_id=9 AND slot_number=17').fetchone() == (assigned,)
        assert conn.execute('SELECT location_slot FROM spools WHERE id=199').fetchone() == (location,)
    finally:
        conn.close()


def test_slice_info_reads_verified_bytes_and_authorized_loaded_slots(review_db, tmp_path):
    from tests.test_contracts.test_education_upload_scheduler_integration import _upload_and_approve
    from modules.organizations.education_slice_info import slice_info
    uploaded = _upload_and_approve(review_db, tmp_path, 'C12')
    review_db.execute(text("UPDATE filament_slots SET filament_type='PETG',loaded_at=CURRENT_TIMESTAMP WHERE printer_id=9")); review_db.commit()
    info = slice_info(uploaded['id'], 9, ADMIN, review_db)
    assert info['plates'][0]['filaments'][0]['material'] == 'PLA'
    assert info['plates'][0]['plate'] == '1'
    assert info['slots'][0]['filament_type'] == 'PETG' and info['slots'][0]['loaded_at']
    assert 'Last recorded' in info['freshness']
    with pytest.raises(OdinError):
        slice_info(uploaded['id'], 9, {'id': 5, 'role': 'admin', 'group_id': 2}, review_db)


def test_slice_info_refuses_changed_payload(review_db, tmp_path):
    from tests.test_contracts.test_education_upload_scheduler_integration import _upload_and_approve
    from modules.organizations.education_slice_info import slice_info
    from pathlib import Path
    uploaded = _upload_and_approve(review_db, tmp_path, 'C12')
    path = Path(review_db.execute(text('SELECT stored_path FROM print_files WHERE id=:id'), {'id': uploaded['print_file_id']}).scalar())
    path.chmod(0o600)
    with path.open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(HTTPException) as error:
        slice_info(uploaded['id'], 9, ADMIN, review_db)
    assert error.value.status_code == 409 and str(path) not in error.value.detail


def test_preserved035_boots_isolated_without_loading_current_migrations(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    runner = Path(__file__).resolve().parents[2] / 'backend/core/schema/preserved035/runner.py'
    code = """
import runpy,sys
namespace=runpy.run_path(sys.argv[1],run_name='ctec_frozen')
bundle,manifest=namespace['prepare_sources']()
from sqlalchemy import create_engine,inspect
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
first=bootstrap_database(engine)
assert 'python:035-production-acceptance' in first['applied']
assert 'python:036-ctec-pilot' not in first['applied']
assert 'archived_at' not in {c['name'] for c in inspect(engine).get_columns('education_submissions')}
assert bootstrap_database(engine)['applied']==[]
assert len(manifest['ledger_ids'])==56
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(runner), str(tmp_path / 'frozen.db')], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
