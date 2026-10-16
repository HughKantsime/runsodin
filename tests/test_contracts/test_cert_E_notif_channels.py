"""Cert-E notification channels: exact outbound requests at the network boundary.

Claims F13-13.1-02..07. Webhooks are configured through the real admin HTTP API
(real SSRF validation, Fernet encryption at rest, event-selection validation),
then fired by both production dispatchers (ORM ``alert_dispatcher`` and daemon
``alert_dispatch``). User-supplied webhook URLs travel the real ``safe_post``
DNS-pinned httpx transport to a real HTTP receiver on 127.0.0.1. The only
substitutions are (a) name resolution of a synthetic ``*.cert-e.test`` host to
127.0.0.1 and (b) admitting exactly 127.0.0.1 through the private-address gate.
Fixed-provider channels (Telegram, Pushover, WhatsApp) are captured at the lowest
transport (``httpx.post`` inside ``trusted_post``) through a real httpx client
with a MockTransport, so the encoded request (URL, method, headers, body) is
asserted exactly. Email uses a real SMTP protocol peer socket on 127.0.0.1.
All credentials are synthetic, generated or literal non-credentials.
"""
import json
import socket
import socketserver
import threading
import urllib.parse
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_license_user_limits import signed_license  # noqa: F401

WAIT = 10


# --------------------------------------------------------------------------
# Network-boundary peers
# --------------------------------------------------------------------------

class Receiver:
    """Real HTTP server on 127.0.0.1 recording every request it receives."""

    def __init__(self):
        self.requests = []
        self.cond = threading.Condition()
        self.status = 200
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _record(self):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else b''
                with receiver.cond:
                    receiver.requests.append({
                        'method': self.command, 'path': self.path,
                        'headers': {k.lower(): v for k, v in self.headers.items()},
                        'body': body,
                    })
                    receiver.cond.notify_all()
                self.send_response(receiver.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

            do_POST = _record
            do_PUT = _record

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def wait(self, count, timeout=WAIT):
        with self.cond:
            assert self.cond.wait_for(lambda: len(self.requests) >= count, timeout), self.requests
            return list(self.requests)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class ProviderTransport:
    """Records requests built by the real httpx client at the lowest transport."""

    def __init__(self):
        self.requests = []
        self.cond = threading.Condition()

    def handler(self, request: httpx.Request):
        with self.cond:
            self.requests.append(request)
            self.cond.notify_all()
        return httpx.Response(200, json={'ok': True})

    def post(self, url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(self.handler)) as client:
            return client.post(url, **kwargs)

    def wait(self, count, timeout=WAIT):
        with self.cond:
            assert self.cond.wait_for(lambda: len(self.requests) >= count, timeout), self.requests
            return list(self.requests)


class SmtpPeer:
    """Minimal real SMTP protocol peer (no AUTH, no STARTTLS) on 127.0.0.1."""

    def __init__(self):
        self.messages = []
        self.envelopes = []
        self.cond = threading.Condition()
        peer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(5)
                self.wfile.write(b'220 synthetic.test ESMTP ready\r\n')
                data, collecting, rcpts = [], False, []
                while True:
                    line = self.rfile.readline()
                    if not line:
                        break
                    if collecting:
                        if line == b'.\r\n':
                            with peer.cond:
                                peer.messages.append(b''.join(data))
                                peer.envelopes.append(list(rcpts))
                                peer.cond.notify_all()
                            collecting, data = False, []
                            self.wfile.write(b'250 accepted\r\n')
                        else:
                            data.append(line[1:] if line.startswith(b'..') else line)
                        continue
                    command = line.split(b' ', 1)[0].strip().upper()
                    if command in (b'EHLO', b'HELO'):
                        self.wfile.write(b'250 synthetic.test\r\n')
                    elif command == b'RCPT':
                        rcpts.append(line.decode().strip())
                        self.wfile.write(b'250 OK\r\n')
                    elif command == b'DATA':
                        collecting = True
                        self.wfile.write(b'354 send data\r\n')
                    elif command == b'QUIT':
                        self.wfile.write(b'221 closing\r\n')
                        break
                    else:
                        self.wfile.write(b'250 OK\r\n')

        self.server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def wait(self, count, timeout=WAIT):
        with self.cond:
            assert self.cond.wait_for(lambda: len(self.messages) >= count, timeout), self.messages
            return [BytesParser(policy=policy.default).parsebytes(m) for m in self.messages]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def receiver():
    peer = Receiver()
    try:
        yield peer
    finally:
        peer.close()


@pytest.fixture
def smtp_peer():
    peer = SmtpPeer()
    try:
        yield peer
    finally:
        peer.close()


@pytest.fixture
def provider(monkeypatch):
    transport = ProviderTransport()
    # trusted_post() does `import httpx as _httpx; _httpx.post(...)`.
    monkeypatch.setattr(httpx, 'post', transport.post)
    return transport


@pytest.fixture
def notif_env(review_db, signed_license, monkeypatch):
    """Real DB-bound modules, real signed Pro entitlement, synthetic Fernet key."""
    from core import db_utils
    import core.license_manager as core_lm
    from modules.notifications import alert_dispatch, quiet_hours
    from modules.notifications.routes import webhooks
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.delenv('ODIN_ITAR_MODE', raising=False)
    monkeypatch.delenv('VAPID_PRIVATE_KEY', raising=False)
    lm, _info = signed_license(tier='pro', features=[])
    monkeypatch.setattr(core_lm, 'get_license', lm.get_license)
    monkeypatch.setattr(webhooks, 'get_license', lm.get_license)
    engine = review_db.get_bind()
    monkeypatch.setattr(db_utils, 'engine', engine)
    monkeypatch.setattr(alert_dispatch, 'engine', engine)
    monkeypatch.setattr(quiet_hours, 'engine', engine)
    monkeypatch.setattr(quiet_hours, '_config_cache', None)
    monkeypatch.setattr(quiet_hours, '_config_ts', 0)
    return review_db


@pytest.fixture
def synthetic_dns(receiver, monkeypatch):
    """Resolve *.cert-e.test to the local receiver; admit only 127.0.0.1."""
    from core import webhook_utils
    real_resolve = webhook_utils._real_getaddrinfo
    real_gate = webhook_utils._is_disallowed_address

    def resolve(host, port, *args, **kwargs):
        if isinstance(host, str) and host.endswith('.cert-e.test'):
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port or 0))]
        return real_resolve(host, port, *args, **kwargs)

    monkeypatch.setattr(webhook_utils, '_real_getaddrinfo', resolve)
    monkeypatch.setattr(webhook_utils, '_is_disallowed_address',
                        lambda address: False if address == '127.0.0.1' else real_gate(address))
    return f'http://hooks.cert-e.test:{receiver.port}'


@pytest.fixture
def admin_api(notif_env):
    """Real webhooks + alerts/SMTP routers as a system administrator session."""
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.notifications.routes import alerts, webhooks
    app = FastAPI()
    app.include_router(webhooks.router)
    app.include_router(alerts.router)

    def session():
        with Session(notif_env.get_bind()) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {
        'id': 3, 'username': 'admin', 'role': 'admin', 'group_id': None,
        'email': 'admin@example.test', 'is_active': True}
    with TestClient(app) as client:
        yield client


def dispatch(path, alert_type, severity, title, message, db):
    """Fire a system-scope alert through a production dispatcher."""
    from core.base import AlertSeverity, AlertType
    from modules.notifications import alert_dispatch, alert_dispatcher
    if path == 'orm':
        alert_dispatcher.dispatch_alert(db, AlertType(alert_type), AlertSeverity(severity), title, message)
    else:
        alert_dispatch.dispatch_alert(alert_type, severity, title, message)


def insert_encrypted_hook(db, kind, address, events):
    """Store a channel row exactly as the API would (Fernet-encrypted URL)."""
    import core.crypto as crypto
    db.execute(text('INSERT INTO webhooks(name,url,webhook_type,alert_types,is_enabled) '
                    'VALUES(:n,:u,:k,:e,1)'),
               {'n': f'Synthetic {kind}', 'u': crypto.encrypt(address), 'k': kind, 'e': json.dumps(events)})
    db.commit()


# --------------------------------------------------------------------------
# F13-13.1-03 Webhooks: Discord / Slack with configurable events
# --------------------------------------------------------------------------

EXPECTED_BODIES = {
    'discord': {'embeds': [{'title': '\U0001f534 Synthetic failure', 'description': 'Synthetic details',
                            'color': 0xef4444, 'footer': {'text': 'O.D.I.N.'}}]},
    'slack': {'blocks': [
        {'type': 'header', 'text': {'type': 'plain_text', 'text': '\U0001f534 Synthetic failure'}},
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'Synthetic details'}}]},
}


@pytest.mark.parametrize('path', ['orm', 'daemon'])
@pytest.mark.parametrize('kind', ['discord', 'slack'])
def test_http_configured_webhook_delivers_provider_payload_only_for_selected_events(
        admin_api, notif_env, receiver, synthetic_dns, path, kind):
    created = admin_api.post('/webhooks', json={
        'name': f'Synthetic {kind}', 'url': f'{synthetic_dns}/{kind}', 'webhook_type': kind,
        'alert_types': ['print_failed']})
    assert created.status_code == 200, created.text
    stored = notif_env.execute(text('SELECT url,alert_types,is_enabled FROM webhooks')).one()
    assert stored.url.startswith('gAAAA') and 'cert-e' not in stored.url  # encrypted at rest
    assert json.loads(stored.alert_types) == ['print_failed'] and stored.is_enabled
    # Unselected event: filtered synchronously before any thread/transport.
    dispatch(path, 'print_complete', 'info', 'Synthetic complete', 'Ignored', notif_env)
    dispatch(path, 'print_failed', 'critical', 'Synthetic failure', 'Synthetic details', notif_env)
    request, = receiver.wait(1)
    assert request['method'] == 'POST' and request['path'] == f'/{kind}'
    assert request['headers']['content-type'] == 'application/json'
    assert request['headers']['host'] == f'hooks.cert-e.test:{receiver.port}'
    assert json.loads(request['body']) == EXPECTED_BODIES[kind]
    assert len(receiver.requests) == 1


def test_http_event_selection_update_changes_delivery(admin_api, notif_env, receiver, synthetic_dns):
    assert admin_api.post('/webhooks', json={'name': 'Synthetic', 'url': f'{synthetic_dns}/d', 'webhook_type': 'discord',
                                             'alert_types': ['print_failed']}).status_code == 200
    hook_id = notif_env.execute(text('SELECT id FROM webhooks')).scalar_one()
    rejected = admin_api.patch(f'/webhooks/{hook_id}', json={'alert_types': [None]})
    assert rejected.status_code == 400
    assert admin_api.patch(f'/webhooks/{hook_id}', json={'alert_types': ['spool_low']}).status_code == 200
    dispatch('daemon', 'print_failed', 'critical', 'Not selected now', '', notif_env)
    dispatch('daemon', 'spool_low', 'warning', 'Synthetic spool', 'Low', notif_env)
    request, = receiver.wait(1)
    assert json.loads(request['body'])['embeds'][0]['title'] == '\U0001f7e1 Synthetic spool'
    assert len(receiver.requests) == 1


def test_discord_test_fire_reaches_receiver(admin_api, notif_env, receiver, synthetic_dns):
    assert admin_api.post('/webhooks', json={'name': 'Synthetic', 'url': f'{synthetic_dns}/fire',
                                             'webhook_type': 'discord'}).status_code == 200
    hook_id = notif_env.execute(text('SELECT id FROM webhooks')).scalar_one()
    response = admin_api.post(f'/webhooks/{hook_id}/test')
    assert response.json() == {'success': True, 'message': 'Test message sent'}
    request, = receiver.wait(1)
    assert request['path'] == '/fire'
    assert json.loads(request['body'])['embeds'][0]['description'] == 'Webhook connection successful!'


@pytest.mark.parametrize('selected', [['print_failed'], ['PRINT_FAILED']])
def test_ui_shaped_event_selection_fires_for_selected_event(admin_api, notif_env, receiver, synthetic_dns, selected):
    # The body WebhookSettings.createWebhook sends after clicking "Print Failed" (canonical id), and the
    # uppercase id older UI builds stored: both select the dispatched lowercase type.
    assert admin_api.post('/webhooks', json={'name': 'UI hook', 'url': f'{synthetic_dns}/ui',
                                             'webhook_type': 'discord', 'alert_types': selected}).status_code == 200
    dispatch('daemon', 'print_failed', 'critical', 'Synthetic failure', 'Synthetic details', notif_env)
    receiver.wait(1, timeout=3)


def test_ui_empty_selection_receives_all_alerts(admin_api, notif_env, receiver, synthetic_dns):
    # "Leave empty to receive all alerts": the UI submits no selection (null) when nothing is
    # selected (CertENotifSettings.cert.test.tsx); an explicit [] still selects nothing.
    assert admin_api.post('/webhooks', json={'name': 'UI hook', 'url': f'{synthetic_dns}/all',
                                             'webhook_type': 'slack', 'alert_types': None}).status_code == 200
    dispatch('daemon', 'print_failed', 'critical', 'Synthetic failure', 'Synthetic details', notif_env)
    receiver.wait(1, timeout=3)


# --------------------------------------------------------------------------
# F13-13.1-04 ntfy: lightweight push via HTTP POST
# --------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['orm', 'daemon'])
@pytest.mark.parametrize('severity,priority', [('critical', 'urgent'), ('warning', 'high'), ('info', 'default')])
def test_ntfy_http_post_body_and_headers(admin_api, notif_env, receiver, synthetic_dns, path, severity, priority):
    created = admin_api.post('/webhooks', json={'name': 'Synthetic ntfy', 'url': f'{synthetic_dns}/odin-topic',
                                                'webhook_type': 'ntfy', 'alert_types': ['print_failed']})
    assert created.status_code == 200, created.text
    dispatch(path, 'print_failed', severity, 'Synthetic title', 'Synthetic body text', notif_env)
    request, = receiver.wait(1)
    assert request['method'] == 'POST' and request['path'] == '/odin-topic'
    assert request['body'] == b'Synthetic body text'
    assert request['headers']['title'] == 'Synthetic title'
    assert request['headers']['priority'] == priority
    assert request['headers']['tags'] == 'printer'


# --------------------------------------------------------------------------
# F13-13.1-05/06/07 fixed-provider channels (Telegram, WhatsApp, Pushover)
# --------------------------------------------------------------------------

@pytest.mark.parametrize('path', ['orm', 'daemon'])
def test_telegram_bot_api_send_message_request(notif_env, provider, path):
    insert_encrypted_hook(notif_env, 'telegram', 'synthetic-bot-token|-100123', ['print_failed'])
    dispatch(path, 'print_failed', 'warning', 'Synthetic title', 'Synthetic details', notif_env)
    request, = provider.wait(1)
    assert request.method == 'POST'
    assert str(request.url) == 'https://api.telegram.org/botsynthetic-bot-token/sendMessage'
    assert request.headers['content-type'] == 'application/json'
    assert json.loads(request.content) == {'chat_id': '-100123', 'text': '\U0001f7e1 *Synthetic title*\nSynthetic details',
                                           'parse_mode': 'Markdown'}


@pytest.mark.parametrize('path', ['orm', 'daemon'])
def test_whatsapp_meta_graph_messages_request(notif_env, provider, path):
    insert_encrypted_hook(notif_env, 'whatsapp', '1098765|15550001111|synthetic-bearer', ['print_failed'])
    dispatch(path, 'print_failed', 'info', 'Synthetic title', 'Synthetic details', notif_env)
    request, = provider.wait(1)
    assert request.method == 'POST'
    assert str(request.url) == 'https://graph.facebook.com/v18.0/1098765/messages'
    assert request.headers['authorization'] == 'Bearer synthetic-bearer'
    assert request.headers['content-type'] == 'application/json'
    assert json.loads(request.content) == {'messaging_product': 'whatsapp', 'to': '15550001111', 'type': 'text',
                                           'text': {'body': '\U0001f535 Synthetic title\nSynthetic details'}}


@pytest.mark.parametrize('path', ['orm', 'daemon'])
@pytest.mark.parametrize('severity,priority', [('critical', '1'), ('warning', '0'), ('info', '0')])
def test_pushover_messages_request_and_priority_mapping(notif_env, provider, path, severity, priority):
    insert_encrypted_hook(notif_env, 'pushover', 'synthetic-user-key|synthetic-app-token', ['print_failed'])
    dispatch(path, 'print_failed', severity, 'Synthetic title', 'Synthetic details', notif_env)
    request, = provider.wait(1)
    assert request.method == 'POST'
    assert str(request.url) == 'https://api.pushover.net/1/messages.json'
    assert request.headers['content-type'] == 'application/x-www-form-urlencoded'
    assert urllib.parse.parse_qs(request.content.decode()) == {
        'token': ['synthetic-app-token'], 'user': ['synthetic-user-key'], 'title': ['Synthetic title'],
        'message': ['Synthetic details'], 'priority': [priority]}


@pytest.mark.parametrize('kind', ['telegram', 'whatsapp', 'pushover'])
def test_unselected_fixed_provider_hooks_send_nothing(notif_env, provider, kind):
    address = {'telegram': 'synthetic-bot-token|1', 'whatsapp': '1|2|synthetic', 'pushover': 'synthetic|synthetic'}[kind]
    insert_encrypted_hook(notif_env, kind, address, ['spool_low'])
    insert_encrypted_hook(notif_env, 'pushover', 'sentinel-user|sentinel-token', ['print_failed'])
    dispatch('daemon', 'print_failed', 'critical', 'Synthetic', 'Synthetic', notif_env)
    request, = provider.wait(1)
    assert 'sentinel-user' in request.content.decode()
    assert len(provider.requests) == 1


@pytest.mark.parametrize('kind,address', [
    ('telegram', '123456789:SyntheticBotToken|-100123'),
    ('whatsapp', '1098765|15550001111|synthetic-bearer'),
    ('pushover', 'synthetic-user-key|synthetic-app-token'),
])
def test_fixed_provider_channel_can_be_configured_via_api(admin_api, notif_env, kind, address):
    response = admin_api.post('/webhooks', json={'name': f'Synthetic {kind}', 'url': address,
                                                 'webhook_type': kind, 'alert_types': ['print_failed']})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize('kind,address', [
    ('telegram', 'https://api.telegram.org/botX'), ('telegram', 'token-without-chat'),
    ('whatsapp', '1098765|15550001111'), ('pushover', 'only-user-key'), ('pushover', 'a|b|c'),
])
def test_fixed_provider_channel_rejects_malformed_credentials(admin_api, kind, address):
    response = admin_api.post('/webhooks', json={'name': 'Bad', 'url': address, 'webhook_type': kind})
    assert response.status_code == 400


def test_api_configured_telegram_channel_delivers(admin_api, notif_env, provider):
    """End to end: created through the API (encrypted), then a dispatched alert reaches the Bot API."""
    assert admin_api.post('/webhooks', json={'name': 'TG', 'url': '123456789:SyntheticBotToken|-100123',
                                             'webhook_type': 'telegram', 'alert_types': ['print_failed']}).status_code == 200
    stored = notif_env.execute(text("SELECT url FROM webhooks WHERE webhook_type='telegram'")).scalar_one()
    assert stored.startswith('gAAAA')
    dispatch('daemon', 'print_failed', 'warning', 'Synthetic title', 'Synthetic details', notif_env)
    request, = provider.wait(1)
    assert str(request.url) == 'https://api.telegram.org/bot123456789:SyntheticBotToken/sendMessage'
    # Changing a URL-type hook to a fixed provider re-validates the stored destination.
    hook_id = notif_env.execute(text("SELECT id FROM webhooks WHERE webhook_type='telegram'")).scalar_one()
    assert admin_api.patch(f'/webhooks/{hook_id}', json={'webhook_type': 'pushover'}).status_code == 400


# --------------------------------------------------------------------------
# F13-13.1-02 Email: SMTP configuration
# --------------------------------------------------------------------------

def configure_smtp(admin_api, peer, **extra):
    body = {'enabled': True, 'host': '127.0.0.1', 'port': peer.port, 'username': '', 'password': '',
            'from_address': 'odin@example.test', 'use_tls': False, **extra}
    response = admin_api.put('/smtp-config', json=body)
    assert response.status_code == 200, response.text


def test_smtp_configuration_round_trip_and_password_encrypted(admin_api, notif_env, smtp_peer):
    configure_smtp(admin_api, smtp_peer, username='synthetic-user', password='synthetic-noncredential')
    stored = notif_env.execute(text("SELECT value FROM system_config WHERE key='smtp_config'")).scalar_one()
    stored = json.loads(stored) if isinstance(stored, str) else stored
    assert stored['password'].startswith('gAAAA') and 'synthetic-noncredential' not in json.dumps(stored)
    shown = admin_api.get('/smtp-config').json()
    assert shown == {'enabled': True, 'host': '127.0.0.1', 'port': smtp_peer.port, 'username': 'synthetic-user',
                     'password_set': True, 'from_address': 'odin@example.test', 'use_tls': False}
    # Saving again with a blank password keeps the stored secret.
    configure_smtp(admin_api, smtp_peer, username='synthetic-user')
    assert admin_api.get('/smtp-config').json()['password_set'] is True


def test_smtp_test_email_endpoint_reaches_peer(admin_api, notif_env, smtp_peer):
    configure_smtp(admin_api, smtp_peer)
    response = admin_api.post('/alerts/test-email')
    assert response.status_code == 200 and response.json()['status'] == 'ok'
    message, = smtp_peer.wait(1)
    assert message['To'] == 'admin@example.test' and message['From'] == 'odin@example.test'
    assert 'Test Alert' in str(message['Subject'])
    assert 'SMTP is configured correctly' in message.get_body(preferencelist=('plain',)).get_content()


@pytest.mark.parametrize('path', ['orm', 'daemon'])
def test_alert_email_delivered_to_preferring_user_via_configured_smtp(admin_api, notif_env, smtp_peer, path):
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    configure_smtp(admin_api, smtp_peer)
    notif_env.add(AlertPreference(user_id=1, alert_type=AlertType.PRINT_FAILED, in_app=True, email=True, browser_push=False))
    notif_env.add(AlertPreference(user_id=2, alert_type=AlertType.PRINT_FAILED, in_app=True, email=False, browser_push=False))
    notif_env.commit()
    dispatch(path, 'print_failed', 'critical', 'Synthetic <b>failure</b>', 'Job & details', notif_env)
    message, = smtp_peer.wait(1)
    assert [r.upper() for r in smtp_peer.envelopes[0]] == ['RCPT TO:<STUDENT@EXAMPLE.TEST>']
    assert message['To'] == 'student@example.test' and message['From'] == 'odin@example.test'
    assert message.get_content_type() == 'multipart/alternative'
    assert 'Synthetic <b>failure</b>' in str(message['Subject'])
    assert 'Job & details' in message.get_body(preferencelist=('plain',)).get_content()
    if path == 'daemon':
        html = message.get_body(preferencelist=('html',)).get_content()
        assert 'Synthetic &lt;b&gt;failure&lt;/b&gt;' in html and 'Job &amp; details' in html


def test_disabled_smtp_never_connects(admin_api, notif_env, smtp_peer):
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    configure_smtp(admin_api, smtp_peer, enabled=False)
    notif_env.add(AlertPreference(user_id=1, alert_type=AlertType.PRINT_FAILED, in_app=True, email=True, browser_push=False))
    notif_env.commit()
    dispatch('daemon', 'print_failed', 'critical', 'Synthetic', 'Synthetic', notif_env)
    assert admin_api.post('/alerts/test-email').status_code == 400
    assert smtp_peer.messages == []


def test_legacy_uppercase_ids_map_to_current_events_at_read_and_match(admin_api, notif_env, receiver, synthetic_dns):
    """Review L2: rows saved by older UI builds (PRINT_FAILED, HMS_ERROR) select the current
    dispatched ids and are listed in canonical form; nothing is rewritten in storage."""
    insert_encrypted_hook(notif_env, 'generic', f'{synthetic_dns}/legacy', ['HMS_ERROR', 'PRINT_FAILED'])
    listed = admin_api.get('/webhooks').json()
    assert json.loads(listed[0]['alert_types']) == ['printer_error', 'print_failed']
    stored = notif_env.execute(text("SELECT alert_types FROM webhooks")).scalar_one()
    assert json.loads(stored) == ['HMS_ERROR', 'PRINT_FAILED']
    dispatch('daemon', 'printer_error', 'critical', 'Synthetic HMS', 'x', notif_env)
    request, = receiver.wait(1)
    assert request['path'] == '/legacy' and json.loads(request['body'])['event'] == 'printer_error'
