"""Real auth/database reads; stored synthetic 3MF and substituted monitor IPC only."""
from types import SimpleNamespace

import pytest
from fastapi import Depends
from sqlalchemy import event, text
from modules.printers import monitor_commands
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db  # noqa: F401
from tests.test_contracts.test_print_objects_cache import stored  # noqa: F401
from tests.test_contracts.test_print_objects_parser import synthetic_members, parse
from tests.test_contracts.test_model_preview_authorization_behavior import principal_headers

READ = '/printers/9/print-objects'


@pytest.fixture
def objects_env(token_client, review_db, stored, monkeypatch):
    from modules.printers import routes_print_objects, routes_controls
    from modules.printers.routes_experimental import experimental_controls_guard
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, issue = token_client
    client.app.include_router(routes_print_objects.router)
    client.app.include_router(routes_controls.router, dependencies=[Depends(experimental_controls_guard)])
    db = review_db
    db.execute(text("INSERT INTO print_files(id,filename,stored_path,file_hash,blob_state,org_id) VALUES(21,'parts.3mf',:p,:h,'present',1)"), {'p': stored.stored_path, 'h': stored.file_hash})
    db.execute(text("INSERT INTO jobs(id,item_name,status,print_file_id,payload_sha256,plate_index,printer_id,charged_to_org_id) VALUES(501,'Parts','printing',21,:h,0,9,1)"), {'h': stored.file_hash})
    db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at,scheduled_job_id) VALUES(912,9,'parts','running',CURRENT_TIMESTAMP,501)"))
    db.execute(text("INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key,observed_print_job_id) VALUES(:t,9,501,:h,:h,:h,912)"), {'t': 'a'*32, 'h': 'a'*64})
    db.commit()
    state = dict(print_job_id=912, gcode_state='RUNNING', skipped=[], fun=None, plate_idx=1)
    calls = []
    monkeypatch.setattr(monitor_commands, 'request_skip_state', lambda pid: state.copy())
    monkeypatch.setattr(monitor_commands, 'request_command', lambda *a, **kw: calls.append((a, kw)) or 'accepted')
    return SimpleNamespace(client=client, issue=issue, db=db, state=state, calls=calls, stored=stored,
                           headers=issue(['read:printers', 'read:models', 'write:printers']))


def read(env):
    response = env.client.get(READ, headers=env.headers)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def test_read_returns_objects_and_never_writes_database(objects_env):
    env = objects_env; statements = []
    # Existing authentication records token use; prime it before measuring handler writes.
    assert read(env)['supported']
    def capture(conn, cursor, statement, params, context, many): statements.append(statement)
    event.listen(env.db.get_bind(), 'before_cursor_execute', capture)
    try:
        result = read(env)
    finally:
        event.remove(env.db.get_bind(), 'before_cursor_execute', capture)
    assert result['supported'] and not result['experimental'] and result['can_skip']
    assert result['print'] == dict(print_job_id=912, job_id=501, name='Parts', plate=1)
    assert [o['id'] for o in result['objects']] == ['15', '300', '79', '90000']
    assert result['max_selectable'] == 3 and result['skipped_reported']
    assert result['picture'] == dict(available=True, aspect=1.25, front_edge=None)
    assert not any(s.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE') for s in statements)
    assert env.calls == []


@pytest.mark.parametrize('sql,code', [
    ("UPDATE print_jobs SET status='completed'", 'not_printing'),
    ("UPDATE printers SET api_type='moonraker' WHERE id=9", 'printer_type_unsupported'),
    ("UPDATE print_jobs SET scheduled_job_id=NULL", 'started_outside_odin'),
    ("DELETE FROM dispatch_attempt_receipts", 'dispatch_not_proven'),
    ("UPDATE jobs SET payload_sha256=NULL WHERE id=501", 'file_unavailable'),
    ("UPDATE jobs SET payload_sha256='wrong' WHERE id=501", 'file_unavailable'),
    ("UPDATE print_files SET blob_state='purged' WHERE id=21", 'file_unavailable'),
    ("UPDATE print_files SET org_id=2 WHERE id=21", 'file_unavailable'),
    ("UPDATE jobs SET plate_index=3 WHERE id=501", 'no_object_list'),
])
def test_reasons_never_release_partial_objects(objects_env, sql, code):
    from modules.printers.print_objects import REASONS
    env = objects_env; env.db.execute(text(sql)); env.db.commit()
    result = read(env)
    assert result['reason_code'] == code and result['reason'] == REASONS[code]
    assert result['objects'] == [] and not result['can_skip'] and not result['picture']['available']
    assert env.calls == []


@pytest.mark.parametrize('kind,code', [('empty','no_object_list'), ('labels','labels_off'), ('one','single_object'), ('many','too_many_objects'), ('busy','preparing'), ('failed','file_unavailable')])
def test_parser_refusals_and_busy(objects_env, monkeypatch, kind, code):
    from modules.models_library import plate_cache, mesh_isolation
    from modules.printers.print_objects import REASONS
    env = objects_env
    if kind == 'busy':
        def busy(*args, **kwargs): raise mesh_isolation.Busy()
        monkeypatch.setattr(plate_cache, 'objects_for_row', busy)
    else:
        document = parse(synthetic_members())
        plate = document['plates'][0]
        if kind == 'empty': plate['objects'] = []
        if kind == 'labels': plate['label_object_enabled'] = False
        if kind == 'one': plate['objects'] = plate['objects'][:1]
        if kind == 'many': plate['objects'] = [dict(identify_id=i, name=None, skipped_at_slice=False, bounds=None) for i in range(1,66)]
        monkeypatch.setattr(plate_cache, 'objects_for_row', lambda row: None if kind == 'failed' else document)
    result = read(env)
    assert result['reason_code'] == code and result['reason'] == REASONS[code].format(n=65)
    assert result['objects'] == []
    if kind == 'busy': assert result['retry_after_seconds'] == mesh_isolation.RETRY_AFTER_SECONDS


@pytest.mark.parametrize('scopes', [['read:printers'], ['read:models'], ['write:printers']])
def test_read_requires_both_scopes(objects_env, scopes):
    env = objects_env
    assert env.client.get(READ, headers=env.issue(scopes)).status_code == 403


def test_other_org_printer_is_404(objects_env):
    env = objects_env
    env.db.execute(text('DELETE FROM education_cost_center_printers WHERE printer_id=9'))
    env.db.execute(text('UPDATE printers SET org_id=2 WHERE id=9')); env.db.commit()
    assert env.client.get(READ, headers=env.headers).status_code == 404


def test_viewer_reads_but_cannot_skip(objects_env):
    env = objects_env
    env.db.execute(text("UPDATE users SET role='viewer' WHERE id=3")); env.db.commit()
    result = read(env)
    assert result['supported'] and not result['can_skip']


def test_controlled_audience_denied(objects_env):
    env = objects_env
    env.db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Restricted')"))
    env.db.execute(text("INSERT INTO controlled_files(file_id,label,audience_id,printer_tag,retention_days,reason,marked_at) VALUES(21,'CUI',1,'Restricted',7,'Restriction',CURRENT_TIMESTAMP)"))
    env.db.commit()
    assert read(env)['reason_code'] == 'file_unavailable'


def test_education_student_cannot_read_another_students_file(objects_env):
    env = objects_env
    env.db.execute(text("UPDATE jobs SET print_file_id=11 WHERE id=501"))
    env.db.execute(text("UPDATE print_files SET stored_path=:p,file_hash=:h WHERE id=11"), {'p':env.stored.stored_path,'h':env.stored.file_hash})
    env.db.commit()
    headers = env.issue(['read:printers','read:models'])
    env.db.execute(text('UPDATE api_tokens SET user_id=4 WHERE id=(SELECT MAX(id) FROM api_tokens)')); env.db.commit()
    result = env.client.get(READ, headers=headers).json()
    assert result['reason_code'] == 'file_unavailable' and result['objects'] == []


@pytest.mark.parametrize('changes,code,reported', [({'skipped':[300]}, None, True),
    ({'print_job_id':911,'skipped':[300]}, None, False),
    ({'fun':'0'}, 'firmware_unsupported', False), ({'fun':format(1 << 49, 'x')}, None, True),
    ({'plate_idx':2}, 'file_unavailable', False)])
def test_monitor_state(objects_env, changes, code, reported):
    env = objects_env; env.state.update(changes)
    result = read(env)
    assert result['reason_code'] == code and result['skipped_reported'] is reported
    if changes == {'skipped':[300]}:
        assert result['objects'][1]['state'] == 'skipped' and result['max_selectable'] == 2


def test_unreachable_monitor_still_returns_list(objects_env, monkeypatch):
    monkeypatch.setattr(monitor_commands,'request_skip_state',lambda pid:None)
    result = read(objects_env)
    assert result['supported'] and not result['skipped_reported']


def test_retained_claim_hash_takes_precedence(objects_env):
    env = objects_env
    env.db.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,token,state) VALUES(9,501,:h,:h,:sent,:t,'accepted')"), {'h':'a'*64,'sent':env.stored.file_hash,'t':'a'*32})
    env.db.execute(text("UPDATE jobs SET payload_sha256=NULL WHERE id=501")); env.db.commit()
    assert read(env)['supported']
    env.db.execute(text("UPDATE printer_dispatch_claims SET payload_sha256=:h"), {'h':'b'*64}); env.db.commit()
    assert read(env)['reason_code'] == 'file_unavailable'


def test_operator_restart_answer_is_not_dispatch_proof(objects_env):
    env = objects_env
    env.db.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=912'))
    env.db.execute(text("INSERT INTO system_config(key,value) VALUES('restart_print:912','{}')"))
    env.db.commit()
    result = read(env)
    assert result['reason_code'] == 'dispatch_not_proven' and result['objects'] == []


@pytest.mark.parametrize('model_org,expected', [(1, 'Readable model'), (2, 'Readable file')])
def test_print_name_uses_reader_visible_display_name(objects_env, model_org, expected):
    env = objects_env
    env.db.execute(text('INSERT INTO models(id,name,org_id) VALUES(801,\'Readable model\',:org)'), {'org': model_org})
    env.db.execute(text("UPDATE print_files SET model_id=801,original_filename='Readable file.gcode.3mf' WHERE id=21"))
    env.db.execute(text("UPDATE jobs SET item_name='odin-dispatch-abcd' WHERE id=501"))
    env.db.commit()
    result = read(env)
    assert result['supported'] and result['print']['name'] == expected
    assert env.db.execute(text('SELECT item_name FROM jobs WHERE id=501')).scalar_one() == 'odin-dispatch-abcd'


@pytest.mark.parametrize('model,has_bounds,front_edge', [
    ('P2S', True, 'bottom'), ('  bambu  lab p2s ', True, 'bottom'), ('N7', True, 'bottom'),
    ('P1S', True, None), ('P2S', False, None),
])
def test_picture_front_edge(objects_env, monkeypatch, model, has_bounds, front_edge):
    from modules.models_library import plate_cache

    env = objects_env
    env.db.execute(text('UPDATE printers SET model=:model WHERE id=9'), {'model': model})
    env.db.commit()
    if not has_bounds:
        document = parse(synthetic_members())
        document['plates'][0]['objects'][0]['bounds'] = None
        monkeypatch.setattr(plate_cache, 'objects_for_row', lambda row: document)
    result = read(env)
    assert result['supported']
    assert result['picture'] == dict(available=has_bounds, aspect=1.25, front_edge=front_edge)
