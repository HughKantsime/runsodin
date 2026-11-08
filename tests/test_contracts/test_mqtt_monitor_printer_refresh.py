"""The MQTT monitor's printer set follows the printers table (spec: mqtt-monitor-printer-refresh)."""
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db  # noqa: F401


class FakeMonitor:
    instances = []

    def __init__(self, ok=True, **kwargs):
        self.kwargs = kwargs
        self.name = kwargs.get('name')
        self.ok = ok
        self.disconnected = False
        FakeMonitor.instances.append(self)

    def connect(self):
        return self.ok

    def disconnect(self):
        self.disconnected = True


@pytest.fixture
def daemon(monkeypatch):
    from modules.printers.monitors import mqtt_monitor as module
    FakeMonitor.instances = []
    monkeypatch.setattr(module, 'PrinterMonitor', lambda **kw: FakeMonitor(**kw))
    monkeypatch.setattr(module, 'MoonrakerMonitor', lambda **kw: FakeMonitor(**kw), raising=False)
    monkeypatch.setattr(module, 'MOONRAKER_AVAILABLE', True)
    d = module.MQTTMonitorDaemon.__new__(module.MQTTMonitorDaemon)
    d.monitors = {}
    d._running = False
    d.load_moonraker_printers = lambda: []
    return d


def bambu(pid, code='12345678', ip='10.0.0.1'):
    return {'id': pid, 'name': f'P{pid}', 'ip': ip, 'serial': f'S{pid}', 'access_code': code}


def start_with(d, rows):
    d._all_printers = rows
    d._all_moonraker = []
    for p in rows:
        m = FakeMonitor(**p)
        d.monitors[p['id']] = m
    return dict(d.monitors)


def test_removed_or_deactivated_printer_is_disconnected_and_never_reconnected(daemon, monkeypatch):
    from modules.printers.monitors import mqtt_monitor as module
    before = start_with(daemon, [bambu(1), bambu(2)])
    daemon.load_printers = lambda: [bambu(1)]
    daemon._check_new_printers()
    assert before[2].disconnected and 2 not in daemon.monitors
    assert not before[1].disconnected and daemon.monitors[1] is before[1]
    assert [p['id'] for p in daemon._all_printers] == [1]
    offline = []
    monkeypatch.setattr(daemon, '_persist_offline', offline.append, raising=False)
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: 0, sleep=lambda _: None))
    count = len(FakeMonitor.instances)
    daemon._check_reconnect()
    assert len(FakeMonitor.instances) == count and offline == []


def test_changed_access_code_reconnects_with_new_value_and_unchanged_rows_do_not_churn(daemon):
    before = start_with(daemon, [bambu(1), bambu(2)])
    daemon.load_printers = lambda: [bambu(1), bambu(2, code='87654321')]
    daemon._check_new_printers()
    assert before[2].disconnected
    assert daemon.monitors[2] is not before[2] and daemon.monitors[2].kwargs['access_code'] == '87654321'
    assert daemon.monitors[1] is before[1] and not before[1].disconnected
    count = len(FakeMonitor.instances)
    daemon._check_new_printers()
    assert len(FakeMonitor.instances) == count


def test_changed_host_reconnects(daemon):
    before = start_with(daemon, [bambu(1)])
    daemon.load_printers = lambda: [bambu(1, ip='10.0.0.9')]
    daemon._check_new_printers()
    assert before[1].disconnected and daemon.monitors[1].kwargs['ip'] == '10.0.0.9'


def test_new_printer_connects_and_failed_connect_is_retried_later(daemon, monkeypatch):
    from modules.printers.monitors import mqtt_monitor as module
    start_with(daemon, [])
    monkeypatch.setattr(module, 'PrinterMonitor', lambda **kw: FakeMonitor(ok=False, **kw))
    daemon.load_printers = lambda: [bambu(3)]
    daemon._check_new_printers()
    assert 3 not in daemon.monitors and [p['id'] for p in daemon._all_printers] == [3]
    monkeypatch.setattr(module, 'PrinterMonitor', lambda **kw: FakeMonitor(**kw))
    daemon._check_new_printers()
    assert 3 in daemon.monitors


def test_failed_reload_keeps_current_monitors(daemon):
    before = start_with(daemon, [bambu(1)])

    def boom():
        raise RuntimeError('database is locked')
    daemon.load_printers = boom
    daemon._check_new_printers()
    assert daemon.monitors[1] is before[1] and not before[1].disconnected
    assert [p['id'] for p in daemon._all_printers] == [1]


def test_moonraker_removed_and_moved_printers(daemon):
    daemon._all_printers = []
    old_a = FakeMonitor(name='K1')
    old_b = FakeMonitor(name='K2')
    daemon.monitors = {10: old_a, 11: old_b}
    daemon._all_moonraker = [{'id': 10, 'name': 'K1', 'host': 'h', 'port': 80},
                             {'id': 11, 'name': 'K2', 'host': 'h2', 'port': 80}]
    daemon.load_printers = lambda: []
    daemon.load_moonraker_printers = lambda: [{'id': 11, 'name': 'K2', 'host': 'h2', 'port': 7125}]
    daemon._check_new_printers()
    assert old_a.disconnected and 10 not in daemon.monitors
    assert old_b.disconnected and daemon.monitors[11].kwargs['port'] == 7125


def test_bambu_loader_skips_inactive_and_non_bambu_rows_but_keeps_legacy_null_type(review_db, monkeypatch):
    from modules.printers.monitors import mqtt_monitor as module
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('ENCRYPTION_KEY', key)
    secret = Fernet(key.encode()).encrypt(b'SER|12345678').decode()
    rows = [(501, 'bambu', 1), (502, 'bambu', 0), (503, 'moonraker', 1), (504, None, 1), (505, 'anycubic_lan', 1), (506, 'Bambu', 1)]
    for pid, api_type, active in rows:
        review_db.execute(text(
            "INSERT INTO printers(id,name,api_type,api_host,api_key,org_id,slot_count,is_active,tags,timelapse_enabled,shared) "
            "VALUES(:id,:name,:t,'10.0.0.5',:k,1,4,:a,'[]',0,0)"),
            {'id': pid, 'name': f'R{pid}', 't': api_type, 'k': secret, 'a': active})
    review_db.commit()
    monkeypatch.setattr(module, 'engine', review_db.get_bind())
    d = module.MQTTMonitorDaemon.__new__(module.MQTTMonitorDaemon)
    loaded = {p['id'] for p in d.load_printers()} & {pid for pid, _, _ in rows}
    assert loaded == {501, 504, 506}


def test_fleet_live_status_skips_inactive_printers(token_client, review_db, monkeypatch):  # noqa: F811
    from modules.printers import routes_crud, routes_status
    client, issue = token_client
    client.app.include_router(routes_crud.router)
    for pid, active in ((601, 1), (602, 0)):
        review_db.execute(text(
            "INSERT INTO printers(id,name,api_type,api_host,api_key,org_id,slot_count,is_active,tags,timelapse_enabled,shared) "
            "VALUES(:id,:name,'bambu','10.0.0.6','x',1,4,:a,'[]',0,0)"), {'id': pid, 'name': f'L{pid}', 'a': active})
    review_db.commit()
    fetched = []
    monkeypatch.setattr(routes_status, '_fetch_printer_live_status',
                        lambda pid, db: fetched.append(pid) or {'printer_id': pid})
    response = client.get('/printers/live-status', headers=issue(['read:printers']))
    assert response.status_code == 200, response.text
    assert 601 in fetched and 602 not in fetched


def test_id_moving_from_moonraker_to_bambu_keeps_the_new_bambu_monitor(daemon):
    # Moonraker printer failed its first connection, then the row became Bambu.
    daemon._all_printers = []
    daemon._all_moonraker = [{'id': 20, 'name': 'X', 'host': 'h', 'port': 80}]
    daemon.load_printers = lambda: [bambu(20)]
    daemon.load_moonraker_printers = lambda: []
    daemon._check_new_printers()
    assert daemon.monitors[20].kwargs.get('serial') == 'S20' and not daemon.monitors[20].disconnected


def test_id_moving_from_bambu_to_moonraker_replaces_the_monitor(daemon):
    before = start_with(daemon, [bambu(21)])
    daemon.load_printers = lambda: []
    daemon.load_moonraker_printers = lambda: [{'id': 21, 'name': 'X', 'host': 'h', 'port': 7125}]
    daemon._check_new_printers()
    assert before[21].disconnected
    assert daemon.monitors[21].kwargs['port'] == 7125 and not daemon.monitors[21].disconnected


def test_changed_serial_reconnects(daemon):
    before = start_with(daemon, [bambu(22)])
    daemon.load_printers = lambda: [dict(bambu(22), serial='NEW')]
    daemon._check_new_printers()
    assert before[22].disconnected and daemon.monitors[22].kwargs['serial'] == 'NEW'


def test_connect_exception_is_contained_and_other_printers_still_connect(daemon, monkeypatch, caplog):
    from modules.printers.monitors import mqtt_monitor as module
    start_with(daemon, [])

    class Exploding(FakeMonitor):
        def connect(self):
            raise RuntimeError('secret-access-code-in-message')
    monkeypatch.setattr(module, 'PrinterMonitor',
                        lambda **kw: Exploding(**kw) if kw['printer_id'] == 30 else FakeMonitor(**kw))
    daemon.load_printers = lambda: [bambu(30), bambu(31)]
    daemon.load_moonraker_printers = lambda: [{'id': 32, 'name': 'K', 'host': 'h', 'port': 80}]
    with caplog.at_level('DEBUG', logger='mqtt_monitor'):
        daemon._check_new_printers()
    assert 30 not in daemon.monitors and 31 in daemon.monitors and 32 in daemon.monitors
    assert [p['id'] for p in daemon._all_printers] == [30, 31]
    assert 'secret-access-code-in-message' not in caplog.text


def test_moonraker_reload_failure_keeps_moonraker_monitors_while_bambu_reconciles(daemon):
    old_k = FakeMonitor(name='K')
    before = start_with(daemon, [bambu(40)])
    daemon.monitors[41] = old_k
    daemon._all_moonraker = [{'id': 41, 'name': 'K', 'host': 'h', 'port': 80}]

    def boom():
        raise OSError('db')
    daemon.load_moonraker_printers = boom
    daemon.load_printers = lambda: []
    daemon._check_new_printers()
    assert before[40].disconnected and 40 not in daemon.monitors
    assert daemon.monitors[41] is old_k and not old_k.disconnected
    assert [p['id'] for p in daemon._all_moonraker] == [41]
