"""SIEM protocol framing, secret handling and outbound restrictions (no network)."""
import hashlib
import hmac
import json
from contextlib import contextmanager
from unittest.mock import MagicMock
import pytest
from modules.system.audit_siem import send
from modules.system.routes_audit_integrity import Destination
from tests.test_contracts.test_audit_integrity_e1 import engine


@pytest.mark.parametrize('endpoint', ['http://example.test', 'https://user:pass@example.test',
    'https://example.test?secret=x', 'https://example.test/#x', 'https://example.test/\n', 'https://example.test:99999/'])
def test_webhook_configuration_refuses_unsafe_endpoints(endpoint):
    with pytest.raises(ValueError):
        Destination(name='owned',kind='webhook',endpoint=endpoint)


def test_https_hmac_pinned_and_no_redirect_or_proxy(monkeypatch):
    import requests
    from core import crypto,itar,webhook_utils
    @contextmanager
    def pinned(url):
        yield ['192.0.2.10']
    monkeypatch.setattr(webhook_utils,'pinned_device_destination',pinned)
    checked=[];monkeypatch.setattr(itar,'enforce_host_destination',checked.append)
    monkeypatch.setattr(crypto,'decrypt',lambda value:'owned-secret')
    session=MagicMock();session.post.return_value.status_code=202
    monkeypatch.setattr(requests,'Session',lambda:session)
    body=b'{"id":1}'
    send({'kind':'webhook','endpoint':'https://example.test/events','secret':'encrypted'},body,'1')
    kwargs=session.post.call_args.kwargs
    assert session.trust_env is False and kwargs['allow_redirects'] is False
    assert checked==['192.0.2.10']
    assert kwargs['headers']['X-ODIN-Signature']=='sha256='+hmac.new(b'owned-secret',body,hashlib.sha256).hexdigest()
    assert kwargs['headers']['X-ODIN-Event-ID']=='1'
    session.post.return_value.close.assert_called_once()


@pytest.mark.parametrize('tls',[False,True])
def test_syslog_rfc5424_octet_framing_and_tls_hostname(monkeypatch,tls):
    import socket,ssl
    from core import itar,webhook_utils
    monkeypatch.setattr(webhook_utils,'device_connect_ip',lambda host,port:'192.0.2.10')
    checked=[];monkeypatch.setattr(itar,'enforce_host_destination',checked.append)
    raw=MagicMock();raw.__enter__.return_value=raw
    connect=MagicMock(return_value=raw);monkeypatch.setattr(socket,'create_connection',connect)
    secure=MagicMock();secure.__enter__.return_value=secure
    context=MagicMock();context.wrap_socket.return_value=secure
    monkeypatch.setattr(ssl,'create_default_context',lambda:context)
    send({'kind':'syslog','endpoint':'syslog://siem.example:6514','tls':tls},b'{"line":"a\\nb"}','42')
    frame=(secure if tls else raw).sendall.call_args.args[0]
    length,content=frame.split(b' ',1)
    assert int(length)==len(content) and content.startswith(b'<134>1 ')
    assert b' odin odin-audit - 42 - ' in content
    assert checked==['192.0.2.10']
    connect.assert_called_once_with(('192.0.2.10',6514),timeout=10)
    if tls: context.wrap_socket.assert_called_once_with(raw,server_hostname='siem.example')


def test_https_private_ca_bundle_and_connection_reuse(monkeypatch):
    from pathlib import Path
    import requests
    from modules.system.audit_siem_transport import Transport
    from core import crypto, itar, webhook_utils
    @contextmanager
    def pinned(url):
        yield ['192.0.2.10']
    monkeypatch.setattr(webhook_utils, 'pinned_device_destination', pinned)
    monkeypatch.setattr(itar, 'enforce_host_destination', lambda ip: None)
    monkeypatch.setattr(crypto, 'decrypt', lambda value: 'owned-secret')
    session = MagicMock(); session.post.return_value.status_code = 202
    factory = MagicMock(return_value=session)
    monkeypatch.setattr(requests, 'Session', factory)
    receiver = {'kind': 'webhook', 'endpoint': 'https://owned.test/events', 'secret': 'sealed',
                'ca_bundle': 'owned PEM fixture'}
    transport = Transport()
    try:
        transport(receiver, b'{}', '1'); transport(receiver, b'{}', '2')
        path = Path(session.post.call_args.kwargs['verify'])
        assert path.read_text() == receiver['ca_bundle']
        factory.assert_called_once()
    finally:
        transport.close()
    assert not path.exists()
    session.close.assert_called_once()


def test_invalid_ca_bundle_is_refused():
    with pytest.raises(ValueError, match='PEM'):
        Destination(name='owned', kind='webhook', endpoint='https://owned.test', ca_bundle='invalid')


def test_syslog_close_keeps_outbox_for_retry(monkeypatch, engine, tier_license):
    from tests.test_contracts.test_audit_integrity_e1 import destination, append
    from modules.system.audit_siem import deliver_once
    from core import itar, webhook_utils
    from sqlalchemy import text
    import socket
    tier_license('enterprise'); destination(engine); append(engine)
    monkeypatch.setattr(webhook_utils, 'device_connect_ip', lambda *a: '192.0.2.10')
    monkeypatch.setattr(itar, 'enforce_host_destination', lambda ip: None)
    raw = MagicMock(); raw.recv.return_value = b''
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: raw)
    assert deliver_once(engine, now=100)
    with engine.connect() as c:
        assert c.execute(text('SELECT attempts FROM audit_siem_outbox')).scalar_one() == 1
        assert c.execute(text('SELECT last_error FROM audit_siem_destinations')).scalar_one() == 'ConnectionError'


def test_syslog_tls_uses_private_ca_and_reconnects_each_event(monkeypatch):
    from core import itar, webhook_utils
    from modules.system.audit_siem_transport import Transport
    import socket, ssl
    monkeypatch.setattr(webhook_utils, 'device_connect_ip', lambda *a: '192.0.2.10')
    monkeypatch.setattr(itar, 'enforce_host_destination', lambda ip: None)
    raw, secure, context = MagicMock(), MagicMock(), MagicMock()
    secure.recv.side_effect = socket.timeout
    context.wrap_socket.return_value = secure
    factory = MagicMock(return_value=raw)
    monkeypatch.setattr(socket, 'create_connection', factory)
    context_factory = MagicMock(return_value=context)
    monkeypatch.setattr(ssl, 'create_default_context', context_factory)
    transport = Transport()
    dest = {'kind': 'syslog', 'endpoint': 'syslog://owned.test', 'tls': True, 'ca_bundle': 'fixture PEM'}
    transport(dest, b'{}', '1'); transport(dest, b'{}', '2')
    assert factory.call_count == 2
    assert context_factory.call_args.kwargs == {'cadata': 'fixture PEM'}
    context.load_default_certs.assert_not_called()
    assert secure.close.call_count == 2
