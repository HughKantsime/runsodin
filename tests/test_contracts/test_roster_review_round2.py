"""D4 round-two regressions for reviewed links and erasure ownership."""
import asyncio
import hashlib
import hmac
import json
import os
import base64

import pytest
from sqlalchemy import text
from core.errors import OdinError
from modules.organizations import education_roster_store as store, education_roster_sync as sync
from modules.organizations import education_roster_providers as providers
from modules.organizations.education_roster_privacy import erase_roster, export_roster
from modules.organizations.routes_education_roster import Configuration
from tests.test_contracts.test_roster_sync import setup, review_db, enable, sample, canvas_config
from tests.test_contracts.test_roster_auth import client
from tests.test_contracts.test_roster_review import confirmations


@pytest.mark.parametrize('first_provider', ['google', 'canvas'])
def test_other_connection_proposes_confirmed_grant_only_link(setup, canvas_config, first_provider):
    db, principal, _ = setup
    google = enable(setup)
    saved = store.save(db, principal, Configuration(provider='canvas', enabled=True, config=canvas_config))
    canvas = store.load(db, saved['id'])
    first, second = (google, canvas) if first_provider == 'google' else (canvas, google)
    roster = {**sample(), 'members': sample()['members'][1:]}
    plan = sync.preview(db, principal, first, roster)
    sync.apply(db, principal, first['id'], plan['preview_token'])
    roster['members'][0]['subject'] = 'second-subject'
    plan = sync.preview(db, principal, second, roster)
    assert plan['account_links'][0]['mode'] == 'grant_only'
    with pytest.raises(OdinError, match='confirmation_required'):
        sync.apply(db, principal, second['id'], plan['preview_token'])
    db.rollback()
    sync.apply(db, principal, second['id'], plan['preview_token'], confirmations(plan))
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_identities')).scalar() == 1
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_grant_links')).scalar() == 1
    # A different subject on this same connection is still a conflict.
    roster['members'][0]['subject'] = 'duplicate-on-same-connection'
    with pytest.raises(OdinError, match='identity_conflict'):
        sync.preview(db, principal, second, roster)


@pytest.mark.parametrize('role', [
    'http://purl.imsglobal.org/vocab/lis/v2/membership/Instructor#TeachingAssistant',
    'Instructor#TeachingAssistant', 'TeachingAssistant',
])
def test_canvas_full_and_short_ta_roles_and_unknown_role_skip(setup, canvas_config, monkeypatch, role):
    db, principal, _ = setup
    saved = store.save(db, principal, Configuration(provider='canvas', enabled=True, config=canvas_config))
    connection = store.load(db, saved['id'])
    async def double(method, url, **kwargs):
        if method == 'POST': return {'access_token':'fixture'}, None
        return {'context':{'id':'course'}, 'members':[
            {'status':'Active', 'user_id':'ta', 'email':'ta@example.test', 'roles':[role]},
            {'status':'Active', 'user_id':'unknown', 'email':'other@example.test', 'roles':['Observer']},
        ]}, None
    monkeypatch.setattr(providers, 'request_json', double)
    data = asyncio.run(providers.canvas_roster(canvas_config, {'id':'course', 'title':'Class',
        'memberships_url':'https://canvas.example.test/nrps'}))
    plan = sync.preview(db, principal, connection, sync.normalize(data, canvas_config))
    assert plan['course']['members'] == [{'subject':'ta', 'email':'ta@example.test', 'role':'manager'}]
    assert plan['skipped'] == [{'subject':'unknown', 'reason':'roster_role_unmatched'}]


def test_link_details_and_decline_through_http(setup, client):
    db, principal, _ = setup
    connection = enable(setup)
    roster = sample()
    roster['members'][0]['email'] = 'student@example.test'
    roster['members'][1]['email'] = 'outsider@example.test'
    plan = sync.preview(db, principal, connection, roster)
    for link in plan['account_links']:
        assert link['username'] == db.execute(text('SELECT username FROM users WHERE id=:u'), {'u':link['user_id']}).scalar()
        member = next(m for m in roster['members'] if m['subject'] == link['subject'])
        assert link['email'] == member['email'] and link['roles'] == [member['role']]
    choices = [{**c, 'decision': 'decline' if c['user_id'] == 4 else 'confirm'} for c in confirmations(plan)]
    response = client.post(f"/api/education/roster/{connection['id']}/import", json={
        'preview_token':plan['preview_token'], 'account_confirmations':choices})
    assert response.status_code == 200, response.text
    assert response.json()['memberships'] == 1
    assert response.json()['skipped'] == [{'subject':'google-student', 'reason':'roster_link_declined'}]
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_grant_links WHERE user_id=4')).scalar() == 0
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_identities WHERE user_id=4')).scalar() == 0


def test_tombstone_is_keyed_and_scoped_to_connection(setup):
    connection = enable(setup)
    actual = store.subject_digest(connection['id'], 'known-subject')
    message = json.dumps(['odin-roster-erasure-v1', connection['id'], 'known-subject'], separators=(',', ':')).encode()
    expected = hmac.new(base64.urlsafe_b64decode(os.environ['ENCRYPTION_KEY']), message, hashlib.sha256).hexdigest()
    assert actual == expected and actual != store.digest('known-subject')
    assert actual != store.subject_digest('another-connection', 'known-subject')


def test_admin_clears_one_tombstone_audited_and_invalidates_preview(setup, client):
    db, principal, _ = setup
    connection = enable(setup)
    roster = {**sample(), 'members':[{'subject':'erased', 'email':'outsider@example.test', 'role':'student'}]}
    plan = sync.preview(db, principal, connection, roster)
    sync.apply(db, principal, connection['id'], plan['preview_token'], confirmations(plan))
    erase_roster(db, 4); db.commit()
    plan = sync.preview(db, principal, connection, roster)
    base = f"/api/education/roster/{connection['id']}/tombstones/clear"
    other_plan = sync.preview(db, principal, connection, roster)
    handle = plan['skipped'][0]['clear_handle']
    assert 'outsider' not in json.dumps(plan)
    response = client.post(base, json={'preview_token': plan['preview_token'], 'clear_handle': handle})
    assert response.status_code == 200, response.text
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_tombstones')).scalar() == 0
    detail = json.loads(db.execute(text("SELECT details FROM audit_logs WHERE action='roster.tombstone_cleared'")).scalar())
    assert detail['user_id'] == principal['id'] and detail['resource_id'] == connection['id']
    with pytest.raises(OdinError, match='preview_stale'):
        sync.apply(db, principal, connection['id'], other_plan['preview_token'])
    db.rollback()
    assert sync.preview(db, principal, connection, roster)['skipped'] == []


@pytest.mark.parametrize('case', ['viewer', 'token', 'foreign', 'unlicensed'])
def test_tombstone_routes_require_licensed_interactive_school_admin(setup, client, tier_license, case):
    db, principal, _ = setup
    connection = enable(setup)
    if case == 'viewer': principal['role'] = 'viewer'
    if case == 'token': principal.update(_auth_kind='user_api_token', _token_scopes=['write:education'])
    if case == 'foreign': principal.update(id=5, group_id=2)
    if case == 'unlicensed': tier_license(None)
    base = f"/api/education/roster/{connection['id']}/tombstones/clear"
    expected = 404 if case == 'foreign' else 403
    assert client.post(base, json={'preview_token': '0'*43, 'clear_handle': '0'*43}).status_code == expected


def test_google_connector_erasure_drops_only_their_refresh_token(setup, client, monkeypatch):
    db, principal, _ = setup
    connection = enable(setup)
    # The configuration creator differs from the person who authorizes Google.
    db.execute(text('UPDATE education_roster_connections SET created_by=4')); db.commit()
    async def double(*args, **kwargs): return 'google-admin', {'refresh_token':'fixture-token'}
    monkeypatch.setattr(providers, 'google_callback', double)
    from urllib.parse import parse_qs, urlsplit
    response = client.post(f"/api/education/roster/{connection['id']}/connect")
    state = parse_qs(urlsplit(response.json()['authorization_url']).query)['state'][0]
    response = client.get(f"/api/auth/classroom/{connection['id']}/callback", params={'state':state,'code':'fixture'}, follow_redirects=False)
    assert response.status_code == 302
    assert export_roster(db, 3)['connected_connections'][0]['id'] == connection['id']
    detail = json.loads(db.execute(text("SELECT details FROM audit_logs WHERE action='roster.connected'")).scalar())
    assert detail['user_id'] == 3
    erase_roster(db, 4); db.commit()
    assert store.load(db, connection['id'])['tokens']['refresh_token'] == 'fixture-token'
    erase_roster(db, 3); db.commit()
    current = store.load(db, connection['id'])
    assert current['enabled'] and current['tokens'] == {} and current['connected_by'] is None
    assert not store.status(current)['connected']
    with pytest.raises(OdinError, match='reconnect_required'):
        asyncio.run(sync.courses(db, current))


def test_declining_link_preserves_unreviewed_existing_grants(setup):
    db, principal, _ = setup
    connection = enable(setup)
    roster = sample()
    plan = sync.preview(db, principal, connection, roster)
    result = sync.apply(db, principal, connection['id'], plan['preview_token'])
    db.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) "
        "VALUES(1,:c,4,'student','active',3)"), {'c':result['cost_center_id']})
    db.commit()
    before = dict(db.execute(text('SELECT * FROM education_cost_center_grants WHERE cost_center_id=:c AND user_id=4'),
                             {'c':result['cost_center_id']}).mappings().one())
    roster['members'].append({'subject':'decline-this', 'email':'outsider@example.test', 'role':'student'})
    plan = sync.preview(db, principal, connection, roster)
    assert plan['revoke_memberships'] == []
    result = sync.apply(db, principal, connection['id'], plan['preview_token'],
                        [{**c, 'decision':'decline'} for c in confirmations(plan)])
    after = dict(db.execute(text('SELECT * FROM education_cost_center_grants WHERE cost_center_id=:c AND user_id=4'),
                            {'c':result['cost_center_id']}).mappings().one())
    assert after == before
    assert result['skipped'] == [{'subject':'decline-this', 'reason':'roster_link_declined'}]
