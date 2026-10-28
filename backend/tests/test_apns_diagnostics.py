"""APNs wire diagnostics and admin endpoint, entirely synthetic/no network."""
import json
import logging
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from modules.push.apns import APNsProvider, PushResult


CASES = [
    (200, '', 'sent'), (400, 'BadDeviceToken', 'bad_device_token'),
    (410, 'Unregistered', 'bad_device_token'),
    (400, 'DeviceTokenNotForTopic', 'topic_mismatch'),
    (400, 'BadTopic', 'topic_mismatch'), (403, 'InvalidProviderToken', 'auth_error'),
    (403, 'ExpiredProviderToken', 'auth_error'), (500, 'InternalServerError', 'other'),
]


@pytest.mark.parametrize('status,reason,expected', CASES)
def test_provider_reason_class_and_no_credential_logging(monkeypatch, caplog, status, reason, expected):
    token = 'synthetic-device-token-never-log'
    key = 'synthetic-signing-key-never-log'
    provider = APNsProvider()
    monkeypatch.setattr(provider, '_is_configured', lambda: True)
    monkeypatch.setattr(provider, '_get_config', lambda: {'bundle_id': 'com.jlls.odin', 'key_content': key})
    monkeypatch.setattr(provider, '_get_jwt', lambda: key)
    requests = []
    def respond(request):
        requests.append(request)
        # Even hostile APNs response text must never reach diagnostics/logs.
        return httpx.Response(status, json={'reason': reason, 'detail': token + key})
    provider._client = httpx.Client(transport=httpx.MockTransport(respond), base_url=provider.host)
    with caplog.at_level(logging.DEBUG):
        result = provider.send_push_result(token, 'Test', 'Body', 'TEST')
    assert result.status == expected
    assert "category=TEST" in caplog.text
    assert "status=" + expected in caplog.text
    assert requests[0].headers['apns-topic'] == 'com.jlls.odin'
    assert json.loads(requests[0].content)['aps']['alert'] == {'title': 'Test', 'body': 'Body'}
    assert token not in caplog.text and key not in caplog.text
    provider.close()


@pytest.mark.parametrize('failure', ['unconfigured', 'transport', 'config'])
def test_provider_local_failures_are_sanitized(monkeypatch, caplog, failure):
    provider = APNsProvider()
    secret = 'synthetic-sensitive-exception'
    monkeypatch.setattr(provider, '_is_configured', lambda: failure != 'unconfigured')
    monkeypatch.setattr(provider, '_get_config', Mock(return_value={'bundle_id': 'com.jlls.odin'}))
    if failure == 'config':
        monkeypatch.setattr(provider, '_get_config', Mock(side_effect=ValueError(secret)))
    monkeypatch.setattr(provider, '_get_jwt', Mock(side_effect=ValueError(secret)))
    result = provider.send_push_result(secret, 'Test', 'Body', 'TEST')
    assert result.status == ('not_configured' if failure == 'unconfigured' else 'other')
    assert secret not in caplog.text


@pytest.fixture
def push_client(review_db, monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.push.routes import push
    from modules.push.models import PushDevice
    for uid in (3, 5):
        review_db.add(PushDevice(user_id=uid, device_id=f'device-{uid}',
                                platform='apns' if uid == 3 else 'apns-sandbox',
                                token=f'synthetic-private-token-{uid}'))
    review_db.commit()
    app = FastAPI()
    app.include_router(push.router, prefix='/api')
    app.include_router(push.router, prefix='/api/v1')
    app.dependency_overrides[get_db] = lambda: review_db
    principal = {'id': 3, 'role': 'admin', 'group_id': 1}
    app.dependency_overrides[get_current_user] = lambda: principal
    provider = Mock()
    provider.send_push_result.return_value = PushResult('sent')
    monkeypatch.setattr(push, 'get_provider', Mock(return_value=provider))
    with TestClient(app) as client:
        yield client, principal, provider


@pytest.mark.parametrize('prefix', ['/api', '/api/v1'])
@pytest.mark.parametrize('result', ['sent', 'not_configured', 'bad_device_token', 'topic_mismatch', 'auth_error', 'other'])
def test_admin_push_wire_results_own_devices_only(push_client, caplog, prefix, result):
    client, principal, provider = push_client
    provider.send_push_result.return_value = PushResult(result)
    response = client.post(prefix + '/push/test')
    assert response.status_code == 200
    payload = response.json()
    assert payload == {'sent': int(result == 'sent'), 'total_devices': 1,
                       'devices': [{'device_id': payload['devices'][0]['device_id'], 'status': result,
                                    'apns_id': None, 'reason': None, 'token_fingerprint': ''}]}
    assert isinstance(payload['devices'][0]['device_id'], int)
    provider.send_push_result.assert_called_once()
    assert provider.send_push_result.call_args.kwargs['device_token'] == 'synthetic-private-token-3'
    assert 'synthetic-private' not in response.text + caplog.text
    assert all(set(item) == {'device_id', 'status', 'apns_id', 'reason', 'token_fingerprint'} for item in payload['devices'])


@pytest.mark.parametrize('role,expected', [('viewer', 403), ('operator', 403), (None, 401)])
def test_test_push_rbac(push_client, role, expected):
    client, principal, provider = push_client
    if role:
        principal['role'] = role
    else:
        principal.clear()
    assert client.post('/api/push/test').status_code == expected
    provider.send_push_result.assert_not_called()


def test_empty_devices(push_client):
    client, principal, provider = push_client
    principal['id'] = 2
    assert client.post('/api/push/test').json() == {'sent': 0, 'total_devices': 0, 'devices': []}
    provider.send_push_result.assert_not_called()


def test_unexpected_provider_exception_is_safe(push_client, caplog):
    client, principal, provider = push_client
    provider.send_push_result.side_effect = ValueError('synthetic-token-key-secret')
    response = client.post('/api/push/test')
    assert response.json()['devices'][0]['status'] == 'other'
    assert 'synthetic-token-key-secret' not in response.text + caplog.text


@pytest.mark.parametrize('registered,configured,count', [(False, False, 0), (True, True, 0), (True, False, 1)])
def test_startup_warning(review_db, monkeypatch, caplog, registered, configured, count):
    from modules.push import apns
    from modules.push.models import PushDevice
    if registered:
        review_db.add(PushDevice(user_id=3, device_id='test', token='synthetic-private-token'))
        review_db.commit()
    monkeypatch.setattr(apns, 'get_provider', lambda: Mock(_is_configured=lambda: configured))
    with caplog.at_level(logging.WARNING):
        apns.warn_if_unconfigured(review_db.get_bind())
    assert caplog.text.count('APNs is not configured') == count
    assert 'synthetic-private-token' not in caplog.text


def test_safe_copy_and_all_category_mappings():
    from modules.push.policy import CATEGORIES, safe_copy
    assert CATEGORIES == {
        'print_complete': 'PRINT_COMPLETE', 'print_failed': 'PRINT_FAILED',
        'print_paused': 'PRINT_PAUSED', 'print_cancelled': 'PRINT_CANCELLED',
        'printer_error': 'HMS_ERROR', 'spaghetti_detected': 'SPAGHETTI_DETECTED',
        'detachment_detected': 'SPAGHETTI_DETECTED', 'first_layer_issue': 'FIRST_LAYER_ISSUE',
        'spool_low': 'SPOOL_LOW', 'job_submitted': 'JOB_APPROVAL_REQUIRED',
        'printer_offline': 'PRINTER_OFFLINE',
    }
    result = safe_copy('Printer error: access_code=12345678 token=private password=hidden '
                       'https://user:pass@example.invalid/path/key?secret=value')
    for value in ('12345678', 'private', 'hidden', 'user:pass', '/path/key', 'secret=value'):
        assert value not in result


def test_route_census_classifies_test_push_admin_only():
    from core import rbac_routes
    # Same classification as other administrator diagnostics, no self-service exemption.
    assert rbac_routes.WRITE_EXEMPTIONS[('POST', '/api/push/test')] == rbac_routes._ADMIN


def test_multiple_own_devices_use_matching_environment(push_client, review_db, monkeypatch):
    from modules.push.models import PushDevice
    from modules.push.routes import push
    client, _, _ = push_client
    review_db.add(PushDevice(user_id=3, device_id='second-own-device', platform='apns-sandbox',
                            token='second-synthetic-token'))
    review_db.commit()
    production = Mock(send_push_result=Mock(return_value=PushResult('sent')))
    sandbox_provider = Mock(send_push_result=Mock(return_value=PushResult('bad_device_token')))
    monkeypatch.setattr(push, 'get_provider', lambda sandbox=False: sandbox_provider if sandbox else production)
    result = client.post('/api/push/test').json()
    assert result['sent'] == 1 and result['total_devices'] == 2
    assert {d['status'] for d in result['devices']} == {'sent', 'bad_device_token'}
    production.send_push_result.assert_called_once()
    sandbox_provider.send_push_result.assert_called_once()


def test_transport_debug_logs_do_not_expose_http2_headers(caplog):
    with caplog.at_level(logging.DEBUG):
        logging.getLogger('hpack.hpack').debug("Adding authorization: bearer synthetic-jwt-private")
        logging.getLogger('hpack.table').debug("Header table: synthetic-jwt-private")
        logging.getLogger('httpx').info('HTTP Request: POST https://api.push.apple.com/3/device/synthetic-path-private?more=synthetic-query-private "HTTP/2 400"')
    for value in ('synthetic-jwt-private', 'synthetic-path-private', 'synthetic-query-private'):
        assert value not in caplog.text


def test_apns_info_proof_with_allowlisted_id_reason_and_fingerprint(monkeypatch, caplog):
    provider = APNsProvider()
    token = 'a'*58 + '123abc'
    identifier = 'f1b4cd68-c604-4a09-901d-75ce896ec134'
    monkeypatch.setattr(provider, '_is_configured', lambda: True)
    monkeypatch.setattr(provider, '_get_config', lambda: {'bundle_id': 'synthetic'})
    monkeypatch.setattr(provider, '_get_jwt', lambda: 'synthetic')
    provider._client = httpx.Client(transport=httpx.MockTransport(lambda request:
        httpx.Response(400, headers={'apns-id': identifier}, json={'reason': 'BadDeviceToken'})), base_url=provider.host)
    with caplog.at_level(logging.INFO):
        result = provider.send_push_result(token, 'Test', 'Body', 'PRINT_PAUSED', data={'alert_type': 'print_paused'})
    assert result.apns_id == identifier and result.reason == 'BadDeviceToken'
    for value in ('category=PRINT_PAUSED', 'alert_type=print_paused', 'status=bad_device_token',
                  'reason=BadDeviceToken', identifier, 'device_ref=123abc'):
        assert value in caplog.text
    assert token not in caplog.text and token[:12] not in caplog.text
    provider.close()


def test_untrusted_provider_metadata_is_removed(push_client, review_db):
    from sqlalchemy import text
    client, _, provider = push_client
    token = 'b'*58 + 'abcdef'
    review_db.execute(text('UPDATE push_devices SET token=:token WHERE user_id=3'), {'token': token})
    review_db.commit()
    provider.send_push_result.return_value = PushResult('sent', 'hostile-id-secret', 'hostile-reason-secret')
    result = client.post('/api/push/test').json()['devices'][0]
    assert result['apns_id'] is None and result['reason'] is None
    assert result['token_fingerprint'] == 'abcdef'
    assert token not in json.dumps(result)


def test_admin_test_push_education_suppression(push_client, review_db, caplog):
    from sqlalchemy import text
    client, _, provider = push_client
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('education_mode','true')"))
    review_db.commit()
    with caplog.at_level(logging.INFO):
        result = client.post('/api/push/test').json()
    assert result['sent'] == 0
    assert result['devices'][0]['status'] == 'education_scope'
    assert 'reason=education_scope' in caplog.text
    provider.send_push_result.assert_not_called()


def test_itar_blocks_before_any_configuration_or_network_read(monkeypatch, caplog):
    from core import itar
    monkeypatch.setattr(itar, 'is_itar_mode', lambda: True)
    provider = APNsProvider()
    configured = Mock(side_effect=AssertionError('Must not read configuration'))
    monkeypatch.setattr(provider, '_get_config', configured)
    with caplog.at_level(logging.INFO):
        result = provider.send_push_result('a'*64, 'Test', 'Body', 'TEST')
    assert result.status == 'not_configured'
    assert 'status=not_configured' in caplog.text
    configured.assert_not_called()


def test_get_preferences_merges_new_categories_without_overriding_opt_out(push_client, review_db):
    from sqlalchemy import text
    client, _, _ = push_client
    review_db.execute(text('UPDATE push_devices SET preferences_json=:prefs WHERE user_id=3'),
                      {'prefs': json.dumps({'categories': {'PRINT_FAILED': False, 'PRINT_PAUSED': False}})})
    review_db.commit()
    result = client.get('/api/push/preferences', params={'device_id': 'device-3'}).json()
    assert result['categories']['PRINT_FAILED'] is False
    assert result['categories']['PRINT_PAUSED'] is False
    assert result['categories']['PRINT_CANCELLED'] is True


def test_rbac_matrix_includes_native_diagnostic():
    from tests.rbac_endpoints_b import ENDPOINTS
    rows = [row for row in ENDPOINTS if row[:2] == ('POST', '/api/push/test')]
    assert len(rows) == 1
    assert rows[0][2]['viewer'] == 403 and rows[0][2]['operator'] == 403
