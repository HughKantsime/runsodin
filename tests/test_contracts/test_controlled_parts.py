"""Real-schema E3 policy, marking, isolation, entitlement and retention contracts."""
import importlib
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from core.controlled_parts import allowed, dispatch_refusal, job_visibility, policies
from modules.organizations.routes_controlled_parts import MarkRequest, change_mark


@pytest.fixture
def database(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'controlled.db'))
    bootstrap_database(engine)
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO groups(id,name) VALUES(1,'Controlled'),(2,'Other')")
        c.exec_driver_sql("INSERT INTO users(id,username,password_hash,role,group_id,is_active) VALUES(1,'member','unused','operator',1,1),(2,'outsider','unused','admin',NULL,1)")
        c.exec_driver_sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure'),(2,'Other')")
        c.exec_driver_sql("INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,1)")
        c.exec_driver_sql("INSERT INTO printers(id,name,tags) VALUES(1,'Allowed','[\"Secure\"]'),(2,'Other','[]')")
        c.exec_driver_sql("INSERT INTO models(id,name,org_id) VALUES(1,'Part',1)")
        c.exec_driver_sql("INSERT INTO print_files(id,filename,model_id,org_id) VALUES(1,'secret.3mf',1,1)")
        c.exec_driver_sql("INSERT INTO jobs(id,item_name,model_id,print_file_id,status,charged_to_org_id) VALUES(1,'Part',1,1,'pending',1),(2,'Public',NULL,NULL,'pending',NULL)")
    with Session(engine) as db:
        yield db
    engine.dispose()


def actor(identifier=1):
    return {'id': identifier, 'role': 'operator' if identifier == 1 else 'admin',
            'group_id': 1 if identifier == 1 else None, '_auth_kind': 'session_jwt'}


def mark(**changes):
    return MarkRequest(**{'label': 'CUI', 'audience_id': 1, 'printer_tag': 'Secure',
                          'retention_days': 7, 'reason': 'Contract restriction', **changes})


def test_migration_repeat_inventory(database):
    from modules.system.postgres_backup_service import canonical_tables
    migration = importlib.import_module('core.schema.migrations.054_controlled_parts')
    migration.validate(database.connection())
    assert {'controlled_files', 'controlled_jobs'} <= canonical_tables()
    database.rollback()
    assert bootstrap_database(database.get_bind())['applied'] == []


@pytest.mark.parametrize('tier', [None, 'pro', 'education'])
def test_license_refusal_audited(database, tier_license, tier):
    tier_license(tier)
    with pytest.raises(HTTPException) as error:
        change_mark(database, 'files', 1, mark(), actor())
    assert error.value.status_code == 403
    assert "Feature 'controlled_parts'" in error.value.detail
    assert database.execute(text('SELECT count(*) FROM controlled_files')).scalar() == 0
    assert database.execute(text("SELECT count(*) FROM audit_logs WHERE action='controlled_part.refused'")).scalar() == 1


def test_file_inheritance_and_no_admin_bypass(database, tier_license):
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    for kind, identifier in [('file', 1), ('job', 1), ('model', 1)]:
        assert allowed(database, kind, identifier, actor())
        assert not allowed(database, kind, identifier, actor(2))
    assert allowed(database, 'job', 2, actor(2))
    assert dispatch_refusal(database, 1, 1) is None
    assert dispatch_refusal(database, 1, 2) == 'controlled_part_printer_group_required'
    tier_license(None)
    assert not allowed(database, 'file', 1, actor(2))
    assert dispatch_refusal(database, 1, 1) == 'controlled_part_feature_not_licensed'
    assert dispatch_refusal(database, 2, 2) is None


def test_live_membership_and_legacy_key(database, tier_license):
    tier_license('enterprise')
    change_mark(database, 'jobs', 1, mark(), actor())
    assert not allowed(database, 'file', 1, dict(actor(), _auth_kind='legacy_global_api_key'))
    database.execute(text('DELETE FROM controlled_audience_members WHERE user_id=1'))
    assert not allowed(database, 'file', 1, actor())


def test_list_filter(database, tier_license):
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    condition, params = job_visibility(actor(2))
    assert database.execute(text('SELECT id FROM jobs WHERE ' + condition), params).scalars().all() == [2]
    condition, params = job_visibility(actor())
    assert database.execute(text('SELECT id FROM jobs WHERE ' + condition), params).scalars().all() == [1, 2]


def test_unmark_audit_and_validation(database, tier_license):
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    change_mark(database, 'files', 1, mark(controlled=False, reason='Released'), actor())
    assert policies(database, 'file', 1) == []
    assert database.execute(text("SELECT count(*) FROM audit_logs WHERE action='controlled_part.changed'")).scalar() == 2
    for invalid in [{'reason': ' '}, {'retention_days': 0}, {'retention_days': True}, {'printer_tag': '\n'}, {'label': ''}]:
        with pytest.raises(ValueError):
            mark(**invalid)


@pytest.mark.parametrize('resource', ['files', 'jobs'])
def test_active_work_cannot_be_reclassified(database, tier_license, resource):
    tier_license('enterprise')
    database.execute(text("UPDATE jobs SET status='printing',printer_id=1 WHERE id=1"))
    database.commit()
    with pytest.raises(HTTPException) as error:
        change_mark(database, resource, 1, mark(), actor())
    assert error.value.detail['code'] == 'controlled_part_work_active'
    assert not policies(database, 'job', 1)


def test_sql_and_runtime_share_job_mark_inheritance(database, tier_license):
    tier_license('enterprise')
    change_mark(database, 'jobs', 1, mark(), actor())
    database.execute(text("INSERT INTO jobs(id,item_name,model_id,print_file_id,status) VALUES(3,'Reprint',1,1,'pending')"))
    database.commit()
    assert not allowed(database, 'job', 3, actor(2))
    condition, params = job_visibility(actor(2))
    assert database.execute(text('SELECT id FROM jobs WHERE ' + condition), params).scalars().all() == [2]


def test_file_preview_and_refusal_audit(database, tier_license):
    from core.model_preview_access import accessible_preview_file
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    assert accessible_preview_file(database, 1, actor()).id == 1
    with pytest.raises(HTTPException) as error:
        accessible_preview_file(database, 1, actor(2))
    assert error.value.detail['code'] == 'controlled_part_group_required'
    database.rollback()
    assert database.execute(text("SELECT count(*) FROM audit_logs WHERE action='controlled_part.refused'")).scalar() == 1


def test_camera_membership_and_public_overlay(database, tier_license):
    from modules.printers.controlled_camera import frame_check
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    database.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1"))
    database.commit()
    assert allowed(database, 'printer', 1, actor())
    assert not allowed(database, 'printer', 1, actor(2))
    assert not allowed(database, 'printer', 1, None)
    assert allowed(database, 'printer', 2, None)
    frame_check(database.get_bind(), 1, {'user_id': 1})
    with pytest.raises(HTTPException):
        frame_check(database.get_bind(), 1, {'user_id': 2})


def test_export_excludes_file_paths_and_full_backups(database, tier_license):
    from core.controlled_parts import path_allowed, assert_full_export
    tier_license('enterprise')
    database.execute(text("UPDATE print_files SET stored_path='/data/uploads/secret.3mf' WHERE id=1"))
    database.commit()
    change_mark(database, 'files', 1, mark(), actor())
    assert not path_allowed(database, '/data/uploads/secret.3mf', actor(2))
    assert path_allowed(database, '/data/uploads/secret.3mf', actor())
    with pytest.raises(HTTPException) as error:
        assert_full_export(database, actor(2))
    assert error.value.detail['code'] == 'controlled_part_export_group_required'


def test_deleted_job_does_not_unprotect_captured_file(database, tier_license):
    from core.controlled_parts import file_visibility
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    condition, params = file_visibility(actor(2), 'f.id')
    database.execute(text('DELETE FROM jobs WHERE id=1'))
    assert database.execute(text('SELECT f.id FROM print_files f WHERE ' + condition), params).all() == []


@pytest.mark.parametrize('bad_tags', ['["secure"]', '"Secure"', '{}', 'broken', None])
def test_dispatch_tag_matching_fails_closed(database, tier_license, bad_tags):
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    database.execute(text('UPDATE printers SET tags=:tags WHERE id=1'), {'tags': bad_tags})
    assert dispatch_refusal(database, 1, 1) == 'controlled_part_printer_group_required'


def test_mark_audit_failure_rolls_back_policy(database, tier_license, monkeypatch):
    import modules.organizations.routes_controlled_parts as route
    tier_license('enterprise')
    def broken(*args, **kwargs):
        raise RuntimeError('audit unavailable')
    monkeypatch.setattr(route, 'log_audit', broken)
    with pytest.raises(RuntimeError):
        change_mark(database, 'files', 1, mark(), actor())
    database.rollback()
    assert not policies(database, 'file', 1)


def test_gdpr_exports_and_erases_mark_author_without_releasing_protection(database, tier_license):
    import asyncio
    from modules.organizations.routes_sessions import export_user_data, erase_user_data
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(reason='Member supplied reason'), actor())
    result = asyncio.run(export_user_data(1, actor(2), database))
    assert result['controlled_part_changes'][0]['reason'] == '[controlled]'
    asyncio.run(erase_user_data(1, actor(2), database))
    saved = database.execute(text('SELECT changed_by,reason,label FROM controlled_files')).one()
    assert saved == (None, '[erased]', 'CUI')
    assert not allowed(database, 'file', 1, actor(2))


def test_foreign_group_and_unknown_resource_rejected(database, tier_license):
    tier_license('enterprise')
    with pytest.raises(HTTPException) as error:
        change_mark(database, 'files', 1, mark(audience_id=2), actor())
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        change_mark(database, 'files', 9999, mark(), actor())
    assert error.value.status_code == 404


def test_marked_source_replacement_requires_explicit_release(database, tier_license):
    from starlette.requests import Request
    from core.controlled_request import guard_source_changes
    tier_license('enterprise')
    change_mark(database, 'jobs', 1, mark(), actor())
    request = Request({'type': 'http', 'method': 'PATCH', 'path': '/api/jobs/1', 'headers': [], 'path_params': {'job_id': 1}})
    with pytest.raises(HTTPException) as error:
        guard_source_changes(database, request, actor(), {'model_id': 2})
    assert error.value.detail['code'] == 'controlled_part_release_required'
    guard_source_changes(database, request, actor(), {'item_name': 'New name'})


def test_migration_refuses_wrong_retention_constraint(tmp_path):
    from core.schema.migrator import MigrationError
    migration = importlib.import_module('core.schema.migrations.054_controlled_parts')
    engine = create_engine('sqlite:///' + str(tmp_path/'wrong.db'))
    with engine.begin() as c:
        migration.apply(c)
        c.exec_driver_sql('ALTER TABLE controlled_files ADD COLUMN unwanted TEXT')
        with pytest.raises(MigrationError, match='column shape'):
            migration.validate(c)
    engine.dispose()


def test_dispatch_latest_variant_fallback_is_also_controlled(database, tier_license):
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    database.execute(text('UPDATE jobs SET print_file_id=NULL WHERE id=1'))
    assert dispatch_refusal(database, 1, 2) == 'controlled_part_printer_group_required'
    assert not allowed(database, 'job', 1, actor(2))


def test_group_deletion_and_bulk_references_are_refused(database, tier_license):
    from core.controlled_parts import assert_group_deletable
    from core.controlled_request import authorize_references
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    for operation in (lambda: assert_group_deletable(database, 1),
                      lambda: authorize_references(database, actor(2), {'job_ids': [1]})):
        with pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 403
    assert_group_deletable(database, 2)


def test_job_mark_cannot_reclassify_a_running_reprint(database, tier_license):
    tier_license('enterprise')
    database.execute(text("INSERT INTO jobs(id,item_name,print_file_id,status,printer_id) VALUES(3,'Reprint',1,'printing',1)"))
    database.commit()
    with pytest.raises(HTTPException) as error:
        change_mark(database, 'jobs', 1, mark(), actor())
    assert error.value.detail['code'] == 'controlled_part_work_active'


def test_real_http_marking_rbac_audit_and_aliases(tmp_path):
    from tests.test_contracts.test_cert_G_harness import run_probe
    result = run_probe('controlled-http', r'''
install_license('enterprise', features=['controlled_parts'])
sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Restricted',1)")
member = seed_user('controlled-operator', 'operator', 1)
viewer_id = seed_user('controlled-viewer', 'viewer', 1)
sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')")
sql("INSERT INTO controlled_audience_members(audience_id,user_id) SELECT 1,id FROM users WHERE group_id=1")
seed_user('controlled-super', 'admin')
operator = login('controlled-operator')
viewer = login('controlled-viewer')
superadmin = login('controlled-super')
sql("INSERT INTO jobs(id,item_name,status,charged_to_org_id) VALUES(1,'Controlled HTTP','pending',1)")
body = dict(label='ITAR', audience_id=1, printer_tag='Secure', retention_days=1, reason='Customer request')
assert viewer.put('/api/controlled-parts/jobs/1', json=body).status_code == 403
assert operator.put('/api/controlled-parts/jobs/1', json=body).status_code == 200
assert viewer.get('/api/v1/controlled-parts/jobs/1').status_code == 200
assert superadmin.get('/api/jobs/1').status_code == 403
assert superadmin.get('/api/controlled-parts/jobs/1').status_code == 403
audit = superadmin.get('/api/audit-logs')
assert audit.status_code == 200, audit.text
assert 'Customer request' not in audit.text
release = dict(body, controlled=False, reason='Customer release')
assert operator.put('/api/v1/controlled-parts/jobs/1', json=release).status_code == 200
assert superadmin.get('/api/jobs/1').status_code == 200
OUT['audits'] = sql("SELECT actor_user_id,details FROM audit_logs WHERE action='controlled_part.changed'")
''', tmp_path)
    assert len(result['audits']) == 2
    assert all(row[0] is not None for row in result['audits'])
    assert result['_net_attempts'] == []


@pytest.mark.parametrize('tier', ['community', 'pro', 'education'])
def test_payload_cannot_enable_enterprise_only_controlled_parts(tier):
    from license_manager import LicenseInfo
    license_info = LicenseInfo()
    license_info.valid = True
    license_info.tier = tier
    license_info.features = ['controlled_parts']
    assert not license_info.has_feature('controlled_parts')
    assert 'controlled_parts' not in license_info.effective_features()


def test_controlled_camera_requires_confirmed_session_reset(database, tier_license, monkeypatch):
    from modules.printers.controlled_camera import prepare_camera
    import modules.printers.route_utils as cameras
    tier_license('enterprise')
    database.execute(text('UPDATE printers SET camera_enabled=1 WHERE id=1'))
    database.commit()
    monkeypatch.setattr(cameras, '_go2rtc_reload', lambda: False)
    assert prepare_camera(database.get_bind(), 1, 1) is None
    change_mark(database, 'jobs', 1, mark(), actor())
    assert prepare_camera(database.get_bind(), 1, 1) == 'controlled_part_camera_sessions_unconfirmed'
    monkeypatch.setattr(cameras, '_go2rtc_reload', lambda: True)
    assert prepare_camera(database.get_bind(), 1, 1) is None
