"""Owned synthetic boundary checks; never contact a provider or live ODIN."""
import asyncio
from types import SimpleNamespace
from cryptography.fernet import Fernet
from sqlalchemy import text
from tests.test_contracts.test_education_oidc_identity import oidc_db, _seed_oidc_config, _oidc_client, _CallbackHandler
from tests.test_contracts.test_oidc_first_setup import setup_client


def test_redirect_pin_ignores_host_and_forwarded_headers(oidc_db, monkeypatch):
    from modules.organizations import routes_oidc, oidc_handler
    _seed_oidc_config(oidc_db)
    captured = []
    class Handler:
        async def get_authorization_url(self):
            return 'https://provider.example.test/authorize', 'fixture-state'
    def factory(config, redirect_uri):
        captured.append(redirect_uri)
        return Handler()
    monkeypatch.setattr(routes_oidc._settings, 'oidc_redirect_uri', 'https://odin.example.test/api/auth/oidc/callback')
    monkeypatch.setattr(oidc_handler, 'create_handler_from_config', factory)
    response = _oidc_client(oidc_db).get('/auth/oidc/login', headers={'host':'attacker.example.test', 'x-forwarded-host':'another.example.test'}, follow_redirects=False)
    assert response.status_code == 302
    assert captured == ['https://odin.example.test/api/auth/oidc/callback']


def test_callback_stores_real_fernet_access_token_not_plaintext(oidc_db, monkeypatch):
    from modules.organizations import oidc_handler
    from core.crypto import decrypt
    _seed_oidc_config(oidc_db)
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setattr(oidc_handler, 'create_handler_from_config', lambda config, redirect_uri: _CallbackHandler())
    response = _oidc_client(oidc_db).get('/auth/oidc/callback?code=fixture-code&state=fixture-state', follow_redirects=False)
    assert response.status_code == 302
    stored = oidc_db.execute(text('SELECT code, access_token FROM oidc_auth_codes')).one()
    assert stored.access_token.startswith('gAAAAA')
    assert decrypt(stored.access_token).count('.') == 2
    assert stored.access_token != decrypt(stored.access_token)
    # The stored code is the browser-bound digest, never the raw one-time code.
    assert not stored.code.startswith('gAAAAA')
    raw = response.headers['location'].split('#oidc_code=', 1)[1]
    from modules.organizations.routes_oidc import oidc_code_key, oidc_state_binding
    assert stored.code == oidc_code_key(raw, oidc_state_binding('fixture-state')) != raw


def test_user_update_whitelists_columns_and_binds_values(setup_client):
    from sqlalchemy.orm import Session
    from modules.organizations.routes_users import update_user
    _, engine, actor = setup_client
    injected = "fixture'; UPDATE users SET role='admin'; --"
    class Body:
        def model_dump(self, **kwargs):
            return {'username': injected, 'id': 999, 'role = \'admin\' --': 'ignored', 'oidc_subject': 'forbidden'}
    with Session(engine) as db:
        asyncio.run(update_user(1, Body(), current_user=actor, db=db))
        row = db.execute(text('SELECT id, username, role, oidc_subject FROM users WHERE id=1')).one()
        assert row.id == 1 and row.username == injected and row.role == 'admin'
        assert row.oidc_subject is None
        assert db.execute(text('SELECT COUNT(*) FROM users')).scalar_one() == 1


def test_webhook_encrypt_helper_roundtrip_and_idempotency(monkeypatch):
    from modules.notifications.routes.webhooks import _encrypt_webhook_url
    from core.crypto import decrypt
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    plain = 'https://hooks.example.test/synthetic-fixture'
    encrypted = _encrypt_webhook_url(plain)
    assert encrypted != plain and encrypted.startswith('gAAAAA')
    assert decrypt(encrypted) == plain
    assert _encrypt_webhook_url(encrypted) == encrypted


def test_printer_response_schema_excludes_key_and_masks_rtsp_credentials():
    from datetime import datetime, timezone
    from modules.printers.schemas import PrinterResponse
    response = PrinterResponse.model_validate({'id':1, 'name':'Fixture', 'api_key':'synthetic-fixture-key', 'camera_url':'rtsp://fixture:fixture@camera.example.test/stream', 'created_at':datetime.now(timezone.utc), 'updated_at':datetime.now(timezone.utc)})
    result = response.model_dump()
    assert 'api_key' not in result
    assert result['camera_url'] == 'rtsp://***@camera.example.test/stream'
    assert 'synthetic-fixture-key' not in str(result)
