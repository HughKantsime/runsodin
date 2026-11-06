"""Round-one decisions: explicit adoption, grant-only links and retained evidence."""
import asyncio
import json
from sqlalchemy import text
import pytest
from core.errors import OdinError
from modules.organizations import education_roster_store as store, education_roster_sync as sync
from modules.organizations.routes_education_roster import Configuration
from tests.test_contracts.test_roster_sync import setup, review_db, enable, sample, imported, canvas_config


def confirmations(plan):
    return [{k: row[k] for k in ('subject', 'user_id', 'mode')} for row in plan['account_links']]


@pytest.mark.parametrize('credential', ['password_hash', 'mfa_enabled', 'oidc_subject', 'oidc_issuer', 'role', None])
def test_google_links_are_individually_confirmed_and_privileged_accounts_get_grants_only(setup, credential):
    db, principal, _ = setup
    connection = enable(setup)
    db.execute(text("UPDATE users SET password_hash='',mfa_enabled=FALSE,oidc_subject=NULL,oidc_issuer=NULL,role='viewer' WHERE id=4"))
    if credential:
        value = {'mfa_enabled': True, 'role':'operator'}.get(credential, 'fixture-credential')
        db.execute(text('UPDATE users SET ' + credential + '=:v WHERE id=4'), {'v': value})
    db.commit()
    roster = {**sample(), 'members':[{'subject':'subject', 'email':'outsider@example.test', 'role':'student'}]}
    plan = sync.preview(db, principal, connection, roster)
    mode = 'grant_only' if credential else 'identity'
    assert plan['create_users'] == 0
    assert plan['account_links'][0]['label'].startswith('Link existing account #4')
    assert plan['account_links'][0]['mode'] == mode
    for wrong in ([], [{'subject':'subject', 'user_id':3, 'mode':mode}]):
        with pytest.raises(OdinError, match='roster_account_confirmation_required'):
            sync.apply(db, principal, connection['id'], plan['preview_token'], wrong)
        db.rollback()
    result = sync.apply(db, principal, connection['id'], plan['preview_token'], confirmations(plan))
    assert db.execute(text('SELECT COUNT(*) FROM education_cost_center_grants WHERE cost_center_id=:c AND user_id=4'),
                      {'c':result['cost_center_id']}).scalar() == 1
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_identities WHERE user_id=4')).scalar() == (0 if credential else 1)
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_grant_links WHERE user_id=4')).scalar() == (1 if credential else 0)
    second = sync.preview(db, principal, connection, roster)
    assert second['account_links'] == [] and second['add_memberships'] == []
    from modules.organizations.routes_roster_auth import imported_user
    if credential:
        with pytest.raises(OdinError, match='roster_import_required'): imported_user(db, connection, 'subject')
    else:
        assert imported_user(db, connection, 'subject')['id'] == 4


def test_canvas_email_never_adopts_even_an_uncredentialed_viewer(setup, canvas_config):
    db, principal, _ = setup
    db.execute(text("UPDATE users SET password_hash='' WHERE id=4")); db.commit()
    saved = store.save(db, principal, Configuration(provider='canvas', enabled=True, config=canvas_config))
    connection = store.load(db, saved['id'])
    roster = {**sample(), 'members':[{'subject':'canvas-person', 'email':'outsider@example.test', 'role':'manager'}]}
    plan = sync.preview(db, principal, connection, roster)
    assert plan['account_links'][0]['mode'] == 'grant_only'
    sync.apply(db, principal, connection['id'], plan['preview_token'], confirmations(plan))
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_identities')).scalar() == 0
    assert db.execute(text('SELECT user_id FROM education_roster_grant_links')).scalar() == 4


def test_two_adoptions_require_each_confirmation(setup):
    db, principal, _ = setup
    connection = enable(setup)
    db.execute(text("UPDATE users SET password_hash='' WHERE id IN (1,4)")); db.commit()
    roster = sample()
    roster['members'][0]['email'] = 'student@example.test'
    roster['members'][1]['email'] = 'outsider@example.test'
    plan = sync.preview(db, principal, connection, roster)
    assert len(plan['account_links']) == 2
    with pytest.raises(OdinError, match='confirmation_required'):
        sync.apply(db, principal, connection['id'], plan['preview_token'], confirmations(plan)[:1])
    db.rollback()
    sync.apply(db, principal, connection['id'], plan['preview_token'], confirmations(plan))
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_identities')).scalar() == 2


def test_unchanged_grants_retain_original_evidence_and_only_leaving_grants_revoke(setup):
    db, principal, _ = setup
    connection, result = imported(setup)
    params = {'c':result['cost_center_id']}
    db.execute(text("UPDATE education_cost_center_grants SET granted_at='2001-01-01 00:00:00',granted_by=1 WHERE cost_center_id=:c"), params)
    db.commit()
    def rows():
        return {r['id']:dict(r) for r in db.execute(text('SELECT * FROM education_cost_center_grants WHERE cost_center_id=:c'), params).mappings()}
    before = rows()
    plan = sync.preview(db, principal, connection, sample())
    sync.apply(db, principal, connection['id'], plan['preview_token'])
    assert rows() == before
    removed = {**sample(), 'members':sample()['members'][:1]}
    plan = sync.preview(db, principal, connection, removed)
    sync.apply(db, principal, connection['id'], plan['preview_token'])
    after = rows()
    for key, grant in after.items():
        if grant['role'] == 'manager': assert grant == before[key]
        else: assert grant['state'] == 'revoked' and grant['granted_at'] == before[key]['granted_at']
    plan = sync.preview(db, principal, connection, sample())
    sync.apply(db, principal, connection['id'], plan['preview_token'])
    assert set(rows()) == set(before)  # Reactivate the retained row, do not replace it.
    assert all(r['state'] == 'active' for r in rows().values())


@pytest.mark.parametrize('email,reason', [(None,'roster_email_missing_or_invalid'), ('','roster_email_missing_or_invalid'),
    ('outside@elsewhere.test','roster_domain_refused')])
def test_bad_emails_are_skipped_and_reported_without_aborting_import(setup, email, reason):
    db, principal, _ = setup
    connection = enable(setup)
    data = sample(); data['members'][0]['email'] = email
    normalized = sync.normalize(data, connection['config'])
    plan = sync.preview(db, principal, connection, normalized)
    assert plan['skipped'] == [{'subject':'google-teacher', 'reason':reason}]
    assert plan['create_users'] == 1
    assert sync.apply(db, principal, connection['id'], plan['preview_token'])['memberships'] == 1


def test_noncredential_settings_retain_tokens_and_credentials_rotation_clears_them(setup):
    db, principal, _ = setup
    connection = enable(setup)
    db.execute(text('UPDATE education_roster_connections SET tokens_encrypted=:t,connected_by=3'), {'t':store.pack({'refresh_token':'fixture-only'})}); db.commit()
    saved = store.save(db, principal, Configuration(provider='google', enabled=True, revision=1,
        config={'allowed_domains':['example.test','another.test']}))
    assert saved['connected'] and store.load(db, connection['id'])['tokens']['refresh_token'] == 'fixture-only'
    assert store.load(db, connection['id'])['connected_by'] == 3
    saved = store.save(db, principal, Configuration(provider='google', enabled=True, revision=2, config={'client_secret':'fixture-rotated'}))
    assert not saved['connected']
    assert store.load(db, connection['id'])['connected_by'] is None


def test_periodic_cleanup_purges_expired_preview_without_new_activity(setup, monkeypatch):
    from core import app_maintenance, db as database
    db, principal, _ = setup
    connection = enable(setup)
    expired = sync.preview(db, principal, connection, sample())
    db.execute(text('UPDATE education_roster_receipts SET expires_at=1')); db.commit()
    future = store.receipt(db, connection['id'], 'preview', {'live':True}); db.commit()
    # Execute the real scheduled loop once, stopping at its next hourly wait.
    waits = []
    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) > 1: raise asyncio.CancelledError()
    monkeypatch.setattr(app_maintenance.asyncio, 'sleep', sleep)
    monkeypatch.setattr(database, 'SessionLocal', lambda: db)
    monkeypatch.setattr(db, 'close', lambda: None)
    with pytest.raises(asyncio.CancelledError): asyncio.run(app_maintenance._periodic_cleanup())
    assert waits == [3600,3600]
    assert db.execute(text('SELECT id FROM education_roster_receipts')).scalars().all() == [store.digest(future)]


def test_canvas_teaching_assistant_maps_to_instructor(canvas_config, monkeypatch):
    from modules.organizations import education_roster_providers as providers
    async def double(method, url, **kw):
        if method == 'POST': return {'access_token':'fixture'}, None
        return {'context':{'id':'course'}, 'members':[
            {'status':'Active', 'user_id':'ta', 'email':'ta@example.test',
             'roles':['http://purl.imsglobal.org/vocab/lis/v2/membership#TeachingAssistant']}]}, None
    monkeypatch.setattr(providers, 'request_json', double)
    data = asyncio.run(providers.canvas_roster(canvas_config, {'id':'course', 'memberships_url':'https://canvas.example.test/nrps'}))
    assert data['members'] == [{'subject':'ta', 'email':'ta@example.test', 'role':'manager'}]


def test_erasing_grant_only_link_tombstones_subject_and_preserves_other_people(setup):
    from modules.organizations.education_roster_privacy import erase_roster, export_roster
    db, principal, _ = setup
    connection, _ = imported(setup)
    roster = {**sample(), 'id':'another-course', 'members':[
        {'subject':'grant-only', 'email':'outsider@example.test', 'role':'student'}]}
    plan = sync.preview(db, principal, connection, roster)
    sync.apply(db, principal, connection['id'], plan['preview_token'], confirmations(plan))
    assert export_roster(db, 4)['grant_links'][0]['subject'] == 'grant-only'
    erase_roster(db, 4); db.commit()
    assert store.load(db, connection['id'])['enabled']
    assert not export_roster(db, 4)['grant_links']
    plan = sync.preview(db, principal, connection, roster)
    assert len(plan['skipped']) == 1
    assert plan['skipped'][0]['reason'] == 'roster_erased_subject'
    assert len(plan['skipped'][0]['clear_handle']) == 43
    assert 'grant-only' not in json.dumps(plan)
    sync.apply(db, principal, connection['id'], plan['preview_token'])
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_identities')).scalar() == 2
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_grant_links')).scalar() == 0
