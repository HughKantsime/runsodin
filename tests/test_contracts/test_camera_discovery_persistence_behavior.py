"""Discovery transport to encrypted DB/config, using owned synthetic sources only."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.test_camera_discovery_claim_acceptance import captured_config
from tests.test_contracts.test_printer_offline_behavior import offline_store, review_db


@pytest.fixture
def discovery_store(offline_store, captured_config, monkeypatch):
    from core.db_utils import _ConnectionAdapter
    from modules.notifications import printer_health
    @contextmanager
    def database():
        raw = offline_store.get_bind().raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()
    monkeypatch.setattr(printer_health, 'get_db', database)
    with offline_store.get_bind().begin() as conn:
        conn.execute(text("UPDATE printers SET api_type='moonraker',api_host='192.168.50.20',camera_enabled=TRUE,is_active=TRUE,camera_url=NULL WHERE id=199"))
    return offline_store, captured_config


@pytest.mark.parametrize('protocol', ['moonraker', 'prusalink', 'elegoo'])
def test_adapter_transport_through_monitor_persists_encrypted_source(discovery_store, monkeypatch, protocol):
    import requests
    from core import crypto
    from modules.printers.monitors import moonraker_monitor, prusalink_monitor, elegoo_monitor
    store, configs = discovery_store
    with store.get_bind().begin() as conn:
        conn.execute(text('UPDATE printers SET api_type=:protocol WHERE id=199'), {'protocol': protocol})
    host = '192.168.50.20'
    calls = []
    if protocol == 'moonraker':
        monitor = moonraker_monitor.MoonrakerMonitor(199, 'Owned', host)
        def get(path):
            calls.append(path)
            return {'result': {'webcams': [{'stream_url': '/webcam/?action=stream'}]}}
        monkeypatch.setattr(monitor.printer, '_get', get)
        monitor.printer._discover_webcam()
        expected = monitor.printer.base_url + '/webcam/?action=stream'
    else:
        if protocol == 'prusalink':
            monitor = prusalink_monitor.PrusaLinkMonitorThread(199, 'Owned', host)
            monkeypatch.setattr(monitor.client, '_get', lambda path: [])
            expected = monitor.client.base_url + '/webcam/?action=snapshot'
        else:
            monitor = elegoo_monitor.ElegooMonitorThread(199, 'Owned', host)
            expected = 'http://' + host + ':8080/?action=stream'
        def head(url, **kwargs):
            calls.append(url)
            return SimpleNamespace(status_code=200)
        from tests.support.device_http import route_device_requests
        route_device_requests(monkeypatch, head=head)
    monitor._discover_and_save_camera()
    stored = store.execute(text('SELECT camera_url FROM printers WHERE id=199')).scalar_one()
    assert calls
    assert stored != expected and crypto.is_encrypted(stored)
    assert crypto.decrypt(stored) == expected
    # HTTP cameras stream through ODIN's pinned loopback relay, never directly.
    from modules.printers.camera_relay import relay_url
    assert configs[-1]['streams']['printer_199'] == 'ffmpeg:' + relay_url(199) + '#video=h264'


@pytest.mark.parametrize('source', [
    'http://192.168.50.21/video', 'http://127.0.0.1/video',
    'file:///tmp/video', 'http://192.168.50.20/video#input=x',
    'http://192.168.50.20/video;exec', 'http://192.168.50.20:bad/video',
    'http://192.168.50.20/video\nsecret',
])
def test_discovery_rejects_unapproved_or_config_injection_source(discovery_store, source):
    from modules.notifications.printer_health import discover_camera
    store, configs = discovery_store
    discover_camera(199, source)
    assert store.execute(text('SELECT camera_url FROM printers WHERE id=199')).scalar_one() is None
    assert not configs


@pytest.mark.parametrize('field,value', [('is_active', False), ('camera_enabled', False)])
def test_discovery_disabled_printer_does_not_persist(discovery_store, field, value):
    from modules.notifications.printer_health import discover_camera
    store, configs = discovery_store
    with store.get_bind().begin() as conn:
        conn.execute(text('UPDATE printers SET ' + field + '=:value WHERE id=199'), {'value': value})
    discover_camera(199, 'http://192.168.50.20/video')
    assert store.execute(text('SELECT camera_url FROM printers WHERE id=199')).scalar_one() is None
    assert not configs


def test_discovery_preserves_explicit_source(discovery_store):
    from core import crypto
    from modules.notifications.printer_health import discover_camera
    store, configs = discovery_store
    original = crypto.encrypt('rtsp://192.168.50.21/explicit')
    with store.get_bind().begin() as conn:
        conn.execute(text('UPDATE printers SET camera_url=:source WHERE id=199'), {'source': original})
    discover_camera(199, 'http://192.168.50.20/video')
    assert store.execute(text('SELECT camera_url FROM printers WHERE id=199')).scalar_one() == original


def test_discovery_encryption_failure_has_no_source_or_secret_logs(discovery_store, monkeypatch, caplog):
    from modules.notifications.printer_health import discover_camera
    store, configs = discovery_store
    monkeypatch.delenv('ENCRYPTION_KEY')
    discover_camera(199, 'http://192.168.50.20/video?token=owned-secret')
    assert store.execute(text('SELECT camera_url FROM printers WHERE id=199')).scalar_one() is None
    assert not configs
    assert 'owned-secret' not in caplog.text


@pytest.mark.parametrize('source', [
    'http://camera-user:owned-secret@192.168.50.20/video',
    'http://192.168.50.20/video?x=a&input=bad',
    'http://169.254.169.254/latest/meta-data/',
    'http://192.168.50.20:70000/video',
])
def test_discovery_rejects_credentials_metadata_and_directives(discovery_store, source, caplog):
    from modules.notifications.printer_health import discover_camera
    store, configs = discovery_store
    discover_camera(199, source)
    assert store.execute(text('SELECT camera_url FROM printers WHERE id=199')).scalar_one() is None
    assert not configs
    assert source not in caplog.text and 'owned-secret' not in caplog.text


def test_discovery_cannot_overwrite_explicit_source_added_after_read(discovery_store, monkeypatch):
    from core import crypto
    from modules.notifications.printer_health import discover_camera
    store, configs = discovery_store
    encrypt = crypto.encrypt
    explicit = encrypt('rtsp://192.168.50.21/explicit')
    def insert_explicit(source):
        with store.get_bind().begin() as conn:
            conn.execute(text('UPDATE printers SET camera_url=:source WHERE id=199'), {'source': explicit})
        return encrypt(source)
    monkeypatch.setattr(crypto, 'encrypt', insert_explicit)
    discover_camera(199, 'http://192.168.50.20:8080/video')
    assert store.execute(text('SELECT camera_url FROM printers WHERE id=199')).scalar_one() == explicit
    assert not configs


def test_discovery_matching_blocked_configured_host_is_rejected(discovery_store):
    from modules.notifications.printer_health import discover_camera
    store, configs = discovery_store
    with store.get_bind().begin() as conn:
        conn.execute(text("UPDATE printers SET api_host='169.254.169.254' WHERE id=199"))
    discover_camera(199, 'http://169.254.169.254/video')
    assert store.execute(text('SELECT camera_url FROM printers WHERE id=199')).scalar_one() is None
    assert not configs
