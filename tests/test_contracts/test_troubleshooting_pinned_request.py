"""The PrusaLink/Moonraker printer check sends the saved key only through the pinned device request."""
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('protocol,path,port', [('moonraker', '/printer/info', 7125), ('prusalink', '/api/v1/status', 80)])
def test_http_check_uses_the_pinned_device_request(monkeypatch, protocol, path, port):
    import httpx
    from core import crypto
    from modules.printers import route_utils
    from modules.system import troubleshooting
    monkeypatch.setattr(troubleshooting, 'safe_destination', lambda host: ('192.0.2.44', None))
    monkeypatch.setattr(crypto, 'decrypt', lambda value: 'synthetic-key')
    monkeypatch.setattr(httpx, 'Client', lambda *a, **k: pytest.fail('a bare client must not carry the saved key'))
    seen = []

    def pinned(client, url, **kwargs):
        seen.append((client, url, kwargs))
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(route_utils, 'pinned_device_get', pinned)
    printer = SimpleNamespace(api_host='printer.example', api_type=protocol, api_key='stored')
    result = troubleshooting.printer_checks(printer)
    assert [(c['stage'], c['status']) for c in result['checks']][-1] == ('http_status', 'pass')
    client, url, kwargs = seen[0]
    assert client is httpx and url == f'http://192.0.2.44:{port}{path}'
    assert kwargs['headers'] == {'X-Api-Key': 'synthetic-key'} and kwargs['timeout'] == 5


def test_a_destination_the_device_policy_refuses_fails_the_check_without_details(monkeypatch):
    from core import crypto
    from core.webhook_utils import DeviceDestinationBlocked
    from modules.printers import route_utils
    from modules.system import troubleshooting
    monkeypatch.setattr(troubleshooting, 'safe_destination', lambda host: ('192.0.2.44', None))
    monkeypatch.setattr(crypto, 'decrypt', lambda value: 'synthetic-key')

    def refused(client, url, **kwargs):
        raise DeviceDestinationBlocked('Host resolves to a blocked address synthetic-key')
    monkeypatch.setattr(route_utils, 'pinned_device_get', refused)
    result = troubleshooting.printer_checks(SimpleNamespace(api_host='printer.example', api_type='moonraker', api_key='stored'))
    last = result['checks'][-1]
    assert last['stage'] == 'http_status' and last['status'] == 'fail'
    assert 'synthetic-key' not in str(result)
