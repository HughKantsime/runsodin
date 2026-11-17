"""Run the HA HTTP client without installing Home Assistant or making network calls."""
import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def api(monkeypatch):
    root = Path(__file__).resolve().parents[2] / 'integrations/home-assistant/custom_components/odin'
    package = types.ModuleType('plate_clear_ha_test')
    package.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    for name in ('const', 'api'):
        spec = importlib.util.spec_from_file_location(f'{package.__name__}.{name}', root / f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status, data):
        self.status, self.data = status, data
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self):
        return self.data


def test_plate_clear_uses_existing_odin_client_once(api):
    session = Mock()
    reply = {'success': True, 'message': 'Job 803 released for dispatch'}
    session.post.return_value = Response(200, reply)
    client = api.OdinClient(session, 'http://odin.example.test/', 'synthetic-test-token')
    assert asyncio.run(client.async_plate_cleared(199)) == reply
    session.post.assert_called_once()
    assert session.post.call_args.args == ('http://odin.example.test/api/v1/printers/199/plate-cleared',)
    assert session.post.call_args.kwargs['headers']['X-API-Key'] == 'synthetic-test-token'


@pytest.mark.parametrize('status,reply,message', [
    (403, {}, 'write:printers.*printers.edit'),
    (401, {}, 'HTTP 401'),
    (409, {'detail': 'Waiting job changed'}, 'Waiting job changed'),
    (503, {'detail': {'message': 'Education policy unavailable'}}, 'Education policy unavailable'),
    (200, {'success': False, 'message': 'Plate was not cleared'}, 'Plate was not cleared'),
])
def test_refusal_is_clear_and_never_retried(api, status, reply, message):
    session = Mock()
    session.post.return_value = Response(status, reply)
    with pytest.raises(api.OdinError, match=message):
        asyncio.run(api.OdinClient(session, 'http://odin.example.test', 'synthetic-test-token').async_plate_cleared(199))
    session.post.assert_called_once()


def test_uncertain_connection_is_not_retried(api):
    session = Mock()
    session.post.side_effect = asyncio.TimeoutError()
    with pytest.raises(api.OdinError, match='Check ODIN before trying again'):
        asyncio.run(api.OdinClient(session, 'http://odin.example.test', 'synthetic-test-token').async_plate_cleared(199))
    session.post.assert_called_once()
