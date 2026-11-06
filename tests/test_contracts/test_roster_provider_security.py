"""D4 hostile provider tokens and the real httpx socket resolution boundary."""
import asyncio
import socket
import threading
import time

import jwt
import pytest
from core.errors import OdinError
from core import webhook_utils
from modules.organizations import education_roster_providers as providers
from tests.test_contracts.test_roster_sync import canvas_config, platform_config, claims


@pytest.mark.parametrize('case,reason', [
    ('tool_key', 'roster_lti_claims_invalid'),
    ('none', 'roster_lti_signature_invalid'),
    ('HS256', 'roster_lti_signature_invalid'),
    ('kid', 'roster_lti_signature_invalid'),
    ('old_iat', 'roster_lti_claims_invalid'),
    ('future_iat', 'roster_lti_claims_invalid'),
    ('multiple_aud', 'roster_lti_claims_invalid'),
    ('message_type', 'roster_lti_claims_invalid'),
    ('version', 'roster_lti_claims_invalid'),
    ('missing_nrps', 'roster_lti_claims_invalid'),
])
def test_hostile_launch_claims_have_exact_refusal_reason(canvas_config, platform_config, monkeypatch, case, reason):
    payload = claims(canvas_config)
    algorithm, signing, kid = 'RS256', platform_config['private_key'], platform_config['key_id']
    if case == 'tool_key': signing = canvas_config['private_key']
    if case == 'none': algorithm, signing = 'none', None
    if case == 'HS256': algorithm, signing = 'HS256', 'fixture-only-hmac-key-with-enough-bytes'
    if case == 'kid': kid = 'unknown-key'
    if case == 'old_iat': payload['iat'] = int(time.time()) - 700
    if case == 'future_iat': payload['iat'] = int(time.time()) + 120
    if case == 'multiple_aud': payload['aud'] = [canvas_config['client_id'], 'another-client']
    if case == 'message_type': payload[providers.LTI + 'message_type'] = 'LtiDeepLinkingRequest'
    if case == 'version': payload[providers.LTI + 'version'] = '1.1'
    if case == 'missing_nrps': del payload[providers.NRPS]
    encoded = jwt.encode(payload, signing, algorithm=algorithm, headers={'kid': kid})
    async def double(*a, **kw): return {'keys': [providers.public_jwk(platform_config)]}, None
    monkeypatch.setattr(providers, 'request_json', double)
    with pytest.raises(OdinError) as error:
        asyncio.run(providers.canvas_launch(canvas_config, {'nonce': 'nonce', 'callback': 'https://odin.example.test/launch'}, encoded))
    assert error.value.extra['reason'] == reason
    assert error.value.status == 403


def test_launch_accepts_clock_skew_within_sixty_seconds(canvas_config, platform_config, monkeypatch):
    payload = claims(canvas_config)
    payload.update(iat=int(time.time()) + 30, exp=int(time.time()) - 30)
    encoded = jwt.encode(payload, platform_config['private_key'], algorithm='RS256', headers={'kid': platform_config['key_id']})
    async def double(*a, **kw): return {'keys': [providers.public_jwk(platform_config)]}, None
    monkeypatch.setattr(providers, 'request_json', double)
    verified, _ = asyncio.run(providers.canvas_launch(canvas_config, {'nonce':'nonce', 'callback':'https://odin.example.test/launch'}, encoded))
    assert verified['sub'] == 'student'


@pytest.mark.parametrize('method,url', [
    ('GET', 'https://canvas.example.test/jwks'),
    ('POST', providers.GOOGLE_TOKEN),
    ('POST', 'https://canvas.example.test/token'),
    ('GET', 'https://canvas.example.test/nrps'),
    ('GET', providers.GOOGLE_API + '/courses'),
    ('GET', providers.GOOGLE_INFO),
])
def test_real_httpx_transport_connects_only_to_validated_ip(monkeypatch, method, url):
    # Real httpx/httpcore and socket.getaddrinfo; stop at connect so no network
    # access is needed. The former AsyncClient path loses the pin in its DNS
    # worker and attempts 127.0.0.1 instead, failing this assertion.
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname
    validation_threads, destinations = [], []
    def validated(value):
        validation_threads.append(threading.get_ident())
        return ['93.184.216.34'], 443, host, 'https'
    monkeypatch.setenv('ODIN_ITAR_MODE', '0')
    monkeypatch.setattr(webhook_utils, '_resolve_and_pin', validated)
    monkeypatch.setattr(webhook_utils, '_real_getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443))])
    def connect(sock, address):
        destinations.append((address, threading.get_ident()))
        raise OSError('Stopped at socket boundary by regression test')
    monkeypatch.setattr(socket.socket, 'connect', connect)
    with pytest.raises(OdinError, match='roster_provider_unavailable'):
        asyncio.run(providers.request_json(method, url))
    assert destinations == [(('93.184.216.34', 443), validation_threads[0])]
    assert validation_threads[0] != threading.get_ident()
