"""Real-app export coverage for user entities, actors, and charged queue jobs."""
import pytest

from tests.test_contracts.test_cert_G_harness import run_probe


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    return run_probe('gdpr-export-audit', r'''
install_license('enterprise')
uid = seed_user('export-operator', 'operator')
other = seed_user('export-other', 'operator')
c = login('export-operator')
response = c.post('/api/jobs', json={'item_name': 'audit-job-marker', 'duration_hours': 1})
assert response.status_code in (200, 201), response.text
job_id = response.json()['id']
foreign = login('export-other').post('/api/jobs', json={'item_name': 'foreign-job-marker', 'duration_hours': 1})
assert foreign.status_code in (200, 201), foreign.text
for action, entity, entity_id, actor in [
    ('auth.login', 'user', uid, None),
    ('account.changed', 'user', uid, other),
    ('actor.only', 'printer', 99, uid),
    ('camera.viewed', 'printer', 99, uid),
    ('timelapse.viewed', 'timelapse', 99, uid),
    ('actor.null_entity', None, None, uid),
    ('actor.null_id', 'user', None, uid),
    ('both.links', 'user', uid, uid),
    ('unrelated', 'user', other, other),
    ('wrong.entity', 'printer', uid, None),
]:
    sql('INSERT INTO audit_logs(action,entity_type,entity_id,actor_user_id,actor_username,actor_auth_kind,details) '
        'VALUES(:a,:e,:i,:u,:n,:k,:d)', a=action, e=entity, i=entity_id, u=actor,
        n=('export-operator' if actor == uid else 'export-other') if actor else None,
        k='bearer_jwt' if actor else None, d='{}')
response = c.get(f'/api/users/{uid}/export')
assert response.status_code == 200, response.text
export = response.json()
OUT['has_job_marker'] = 'audit-job-marker' in response.text
OUT['has_foreign_marker'] = 'foreign-job-marker' in response.text
OUT['submitted'] = [j['id'] for j in export['jobs_submitted']]
OUT['charged'] = [j['id'] for j in export.get('jobs_charged', [])]
OUT['job_id'] = job_id
OUT['entities'] = export['audit_log_entries']
OUT['actors'] = export['audit_actions_performed']
''', tmp_path_factory.mktemp('gdpr-export-audit'))


def test_export_preserves_user_entity_and_actor_links(exported):
    result = exported
    entities, actors = result['entities'], result['actors']
    assert {'auth.login', 'account.changed', 'both.links'} <= {r['action'] for r in entities}
    assert {'job.created', 'camera.viewed', 'timelapse.viewed', 'actor.only', 'actor.null_entity', 'actor.null_id'} <= {r['action'] for r in actors}
    assert not {r['id'] for r in entities} & {r['id'] for r in actors}
    assert not {'unrelated', 'wrong.entity'} & {r['action'] for r in entities + actors}
    assert all(not {'actor_user_id', 'actor_username', 'actor_auth_kind'} & set(r) for r in entities + actors)
    assert result['_net_attempts'] == []


def test_export_includes_charged_queue_job_marker(exported):
    result = exported
    assert result['has_job_marker']
    assert not result['has_foreign_marker']
    assert result['submitted'] == []
    assert result['charged'] == [result['job_id']]
    assert result['_net_attempts'] == []
