"""Skip requests are checked again, sent once, and audited without names."""
import json
import pytest
from sqlalchemy import text
from modules.printers import monitor_commands
from tests.test_contracts.test_print_objects_route import objects_env, token_client, review_db, stored  # noqa: F401

POST = '/printers/9/skip-objects'


def post(env, body=None):
    return env.client.post(POST, headers=env.headers, json=body or dict(print_job_id=912, object_ids=['15']))


def audits(env):
    return [(r[0], json.loads(r[1])) for r in env.db.execute(text("SELECT action,details FROM audit_logs WHERE action LIKE 'printer.skip_objects_%' ORDER BY id"))]


@pytest.mark.parametrize('body', [None, [], 'x', {}, {'object_ids':[15]},
    {'print_job_id':True,'object_ids':[15]}, {'print_job_id':0,'object_ids':[15]},
    {'print_job_id':912,'object_ids':[]}, {'print_job_id':912,'object_ids':[True]},
    {'print_job_id':912,'object_ids':[1.5]}, {'print_job_id':912,'object_ids':[-1]},
    {'print_job_id':912,'object_ids':['']}, {'print_job_id':912,'object_ids':['x'*129]},
    {'print_job_id':912,'object_ids':['15',15]}, {'print_job_id':912,'object_ids':list(range(65))},
    {'print_job_id':912,'object_ids':{}}])
def test_body_validation(objects_env, body):
    env = objects_env
    response = env.client.post(POST, headers=env.headers, json=body)
    assert response.status_code == 422 and response.json()['error']['code'] == 'invalid_request'
    assert env.calls == [] and audits(env) == []


@pytest.mark.parametrize('role,scopes', [('viewer',['write:printers']), ('admin',['read:printers','read:models']), ('operator',['write:jobs'])])
def test_authorization_sends_nothing(objects_env, role, scopes):
    env = objects_env
    env.db.execute(text('UPDATE users SET role=:role WHERE id=3'), {'role':role}); env.db.commit()
    response = env.client.post(POST, headers=env.issue(scopes), json=dict(print_job_id=912,object_ids=[15]))
    assert response.status_code == 403 and env.calls == [] and audits(env) == []


@pytest.mark.parametrize('body,status,code', [
    (dict(print_job_id=913,object_ids=[15]),409,'print_changed'),
    (dict(print_job_id=912,object_ids=['0']),422,'unknown_object'),
    (dict(print_job_id=912,object_ids=['015']),422,'unknown_object'),
    (dict(print_job_id=912,object_ids=[15,300,79,90000]),409,'would_skip_everything')])
def test_stale_unknown_and_skip_everything(objects_env, body, status, code):
    env = objects_env
    response = post(env,body)
    assert response.status_code == status and response.json()['error']['code'] == code
    assert env.calls == [] and audits(env) == []


def test_all_already_skipped_has_no_side_effect(objects_env):
    env = objects_env; env.state['skipped'] = [15]
    response = post(env)
    assert response.json() == dict(success=True,outcome='nothing_to_send',sent=[],already_skipped=['15'],message='That object is already skipped.')
    assert env.calls == [] and audits(env) == []


def test_only_remaining_ids_sent_once_with_audit(objects_env):
    env = objects_env; env.state['skipped'] = [15]
    response = post(env, dict(print_job_id=912,object_ids=[15,'300']))
    assert response.status_code == 200 and response.json()['sent'] == ['300']
    assert env.calls == [((9,'skip_objects'),dict(print_job_id=912,object_ids=[300]))]
    [(action,details)] = audits(env)
    assert action == 'printer.skip_objects_requested'
    assert details == dict(print_job_id=912,job_id=501,source='bambu_3mf',object_ids=['300'],object_ids_not_sent=[],already_skipped=['15'],outcome='accepted',actor_token_id=env.db.execute(text('SELECT MAX(id) FROM api_tokens')).scalar())
    assert 'copy.stl' not in json.dumps(details)


@pytest.mark.parametrize('outcome,status,code,action', [('unavailable',503,'not_connected','failed'),
    ('refused',409,'print_changed','failed'), ('unknown',503,'outcome_unknown','unconfirmed')])
def test_monitor_outcomes(objects_env, monkeypatch, outcome, status, code, action):
    env = objects_env
    monkeypatch.setattr(monitor_commands,'request_command',lambda *a,**kw:env.calls.append((a,kw)) or outcome)
    response = post(env)
    assert response.status_code == status and response.json()['error']['code'] == code
    assert response.json()['error']['retriable'] is False
    assert len(env.calls) == 1 and audits(env)[0][0] == 'printer.skip_objects_'+action


def test_audit_commit_failure_is_unknown_without_retry(objects_env, monkeypatch):
    from sqlalchemy.orm import Session
    env = objects_env
    def fail(self): raise RuntimeError('synthetic commit failure')
    monkeypatch.setattr(Session, 'commit', fail)
    response = post(env)
    assert response.status_code == 503 and response.json()['error']['code'] == 'outcome_unknown'
    assert len(env.calls) == 1


def test_busy_retry_after_and_unsupported(objects_env, monkeypatch):
    from modules.models_library import plate_cache, mesh_isolation
    env = objects_env
    def busy(*a,**kw): raise mesh_isolation.Busy()
    monkeypatch.setattr(plate_cache, 'objects_for_row', busy)
    response = post(env)
    assert response.status_code == 409 and response.json()['error']['code'] == 'preparing'
    assert response.headers['retry-after'] == str(mesh_isolation.RETRY_AFTER_SECONDS) and env.calls == []


def test_gate_and_visibility_precede_body_checks(objects_env):
    env = objects_env
    env.db.execute(text("DELETE FROM system_config WHERE key='experimental_printer_actions'"));env.db.commit()
    assert post(env, {'bad':1}).json()['detail']['code'] == 'experimental_not_enabled'
    env.db.execute(text('DELETE FROM education_cost_center_printers WHERE printer_id=9'))
    env.db.execute(text('UPDATE printers SET org_id=2 WHERE id=9'));env.db.commit()
    assert post(env, {'bad':1}).status_code == 404
    assert env.calls == []


def test_skip_every_remaining_object_after_previous_skip(objects_env):
    env=objects_env;env.state['skipped']=[15,300]
    response=post(env,dict(print_job_id=912,object_ids=[15,79,90000]))
    assert response.status_code==409 and response.json()['error']['code']=='would_skip_everything'
    assert env.calls==[]


@pytest.mark.parametrize('api_type,expected', [
    ('bambu', "Skipping objects is Experimental on Bambu printers and is off for this printer. A platform administrator can turn it on in this printer's Experimental features."),
    ('octoprint', "This printer is Experimental: ODIN's support for its type is built from public documentation and has not been verified on real hardware. Job sending and printer controls stay off until an administrator turns them on for this printer. Monitoring still works."),
])
def test_gate_names_only_the_gated_capability(objects_env, api_type, expected):
    env = objects_env
    env.db.execute(text("UPDATE printers SET api_type=:type WHERE id=9"), {'type': api_type})
    env.db.execute(text("DELETE FROM system_config WHERE key='experimental_printer_actions'"))
    env.db.commit()
    response = post(env)
    assert response.status_code == 409
    assert response.json()['detail'] == {'code': 'experimental_not_enabled', 'message': expected}
    assert env.calls == []


@pytest.mark.parametrize('ids,expected', [
    ([15], 'Skip sent for 1 object. It shows as skipped in the list once the printer confirms.'),
    ([15, 300], 'Skip sent for 2 objects. They show as skipped in the list once the printer confirms.'),
])
def test_receipt_number_agreement(objects_env, ids, expected):
    response = post(objects_env, dict(print_job_id=912, object_ids=ids))
    assert response.status_code == 200
    assert response.json()['message'] == expected


def test_plural_already_skipped_receipt(objects_env):
    objects_env.state['skipped'] = [15, 300]
    response = post(objects_env, dict(print_job_id=912, object_ids=[15, 300]))
    assert response.json()['message'] == 'Those objects are already skipped.'
    assert objects_env.calls == [] and audits(objects_env) == []
