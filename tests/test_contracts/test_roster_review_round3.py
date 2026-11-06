"""Preview-scoped erasure suppression handles never expose erased identities."""
import json
import pytest
from sqlalchemy import text
from modules.organizations import education_roster_store as store, education_roster_sync as sync
from tests.test_contracts.test_roster_sync import setup, review_db, enable, sample, canvas_config
from tests.test_contracts.test_roster_auth import client
from modules.organizations.routes_education_roster import Configuration


def erased_preview(setup):
    db, principal, _ = setup
    connection = enable(setup)
    data = sample()
    for member in data['members']:
        db.execute(text('INSERT INTO education_roster_tombstones(connection_id,subject_hash) VALUES(:c,:s)'),
                   {'c': connection['id'], 's': store.subject_digest(connection['id'], member['subject'])})
    db.commit()
    return connection, data, sync.preview(db, principal, connection, data)


def test_handles_are_private_fresh_and_clear_only_the_selected_skip(setup, client):
    db, principal, _ = setup
    connection, data, plan = erased_preview(setup)
    other = sync.preview(db, principal, connection, data)
    handles = [s['clear_handle'] for s in plan['skipped']]
    assert len(set(handles)) == 2
    assert set(handles).isdisjoint(s['clear_handle'] for s in other['skipped'])
    cached = store.unpack(db.execute(text('SELECT payload_encrypted FROM education_roster_receipts WHERE id=:id'),
                                    {'id': store.digest(plan['preview_token'])}).scalar_one())
    for member in data['members']:
        assert member['subject'] not in json.dumps(plan) + json.dumps(cached)
        assert member['email'] not in json.dumps(plan) + json.dumps(cached)
        assert store.subject_digest(connection['id'], member['subject']) not in json.dumps(plan)
    url = f"/api/education/roster/{connection['id']}/tombstones/clear"
    body = {'preview_token': plan['preview_token'], 'clear_handle': handles[0]}
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'private, no-store'
    assert client.post(url, json=body).status_code == 409  # single use
    db.rollback()
    current = sync.preview(db, principal, connection, data)
    assert current['course']['members'] == [data['members'][0]]
    assert len(current['skipped']) == 1
    detail = db.execute(text("SELECT details FROM audit_logs WHERE action='roster.tombstone_cleared'")).scalar_one()
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert detail['subject_hash'] == store.subject_digest(connection['id'], data['members'][0]['subject'])
    assert detail['user_id'] == principal['id']


@pytest.mark.parametrize('change,expected', [
    ('handle', 404), ('other_preview', 404), ('expired', 409), ('actor', 409),
    ('connection', 409), ('revision', 409), ('local_state', 409),
])
def test_clear_refuses_unbound_expired_or_stale_handles(setup, client, canvas_config, change, expected):
    db, principal, _ = setup
    connection, data, plan = erased_preview(setup)
    cid = connection['id']
    handle = plan['skipped'][0]['clear_handle']
    if change == 'handle':
        handle = 'x' * 43
    if change == 'other_preview':
        handle = sync.preview(db, principal, connection, data)['skipped'][0]['clear_handle']
    if change == 'expired':
        db.execute(text('UPDATE education_roster_receipts SET expires_at=0'))
    if change == 'actor':
        db.execute(text('UPDATE education_roster_receipts SET actor_id=4'))
    if change == 'connection':
        cid = store.save(db, principal, Configuration(provider='canvas', enabled=True, config=canvas_config))['id']
    if change == 'revision':
        db.execute(text('UPDATE education_roster_connections SET revision=revision+1'))
    if change == 'local_state':
        db.execute(text("UPDATE users SET email='changed@example.test' WHERE id=4"))
    db.commit()
    response = client.post(f'/api/education/roster/{cid}/tombstones/clear', json={
        'preview_token': plan['preview_token'], 'clear_handle': handle})
    assert response.status_code == expected, response.text
    db.rollback()
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_tombstones')).scalar() == 2
    assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='roster.tombstone_cleared'")).scalar() == 0
