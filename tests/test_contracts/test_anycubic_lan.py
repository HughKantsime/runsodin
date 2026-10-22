"""Fixture replay and monitor-only stock Anycubic contract evidence."""
import base64
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from modules.printers.adapters.anycubic_lan import AnycubicLANPrinter, decrypt_handshake
from modules.printers.parsing.anycubic_lan import parse_report, filament_slots

FIXTURES = Path(__file__).resolve().parents[2] / 'backend/tests/fixtures/anycubic_lan'
TOPIC = 'anycubic/anycubicCloud/v1/printer/public/20025/synthetic-device/{}/report'


def report(kind, data, old=None):
    return parse_report(TOPIC.format(kind), {'action': 'irrelevant', 'data': data}, old)


@pytest.mark.parametrize('capture', ['smoke-40s', 'idle-600s'])
def test_phase_a_replay(capture):
    state = {}
    for line in (FIXTURES / capture / 'messages.jsonl').read_text().splitlines():
        message = json.loads(line)
        if message['dir'] == 'in':
            state = parse_report(message['topic'], message['payload'], state)
    assert state['internal_state'] == 'IDLE'
    assert state['firmware_version'] == '2.7.2.7'
    assert 23 <= state['nozzle_temp'] <= 26
    slots = filament_slots(state['boxes'])
    assert [s['slot_number'] for s in slots] == [1, 2, 3, 4]
    assert [s['color_hex'] for s in slots] == ['FF6A14', '6558B1', 'EF60A3', '0047BB']
    assert all(s['material'] == 'PLA' and s['remaining'] is None and s['loaded'] for s in slots)
    assert slots[0]['box_model'] == 'ACE Pro'
    assert 'urls' not in state and 'deviceId' not in state


def test_handshake_decrypt_synthetic():
    token, local = '0123456789ABCDEFfedcba9876543210', 'synthetic-iv'
    value = {'username': 'synthetic-user', 'password': 'synthetic-password'}
    pad = PKCS7(128).padder()
    padded = pad.update(json.dumps(value).encode()) + pad.finalize()
    enc = Cipher(algorithms.AES(token[16:32].encode()), modes.CBC(local.encode().ljust(16, b'\0'))).encryptor()
    encrypted = base64.b64encode(enc.update(padded) + enc.finalize())
    assert decrypt_handshake(token, local, encrypted) == value
    with pytest.raises(Exception):
        decrypt_handshake(token, 'wrong-iv', encrypted)


@pytest.mark.parametrize('action', ['pause', 'resume', 'stop', 'start', 'update', 'setDry', 'setAutoFeed', 'startCapture', '', None])
def test_publish_denies_controls(action):
    with pytest.raises(ValueError, match='monitor-only'):
        AnycubicLANPrinter('192.0.2.5').publish('print', action)


def test_poll_allowlist():
    printer = AnycubicLANPrinter('192.0.2.5')
    sent = []
    printer._client = SimpleNamespace(publish=lambda topic, body: (sent.append((topic, json.loads(body))) or SimpleNamespace(rc=0)))
    printer._connected = True
    printer._identity = ('20025', 'synthetic-device')
    assert printer.poll()
    assert {(t.rsplit('/', 1)[-1], p['action']) for t, p in sent} == {('info','query'), ('tempature','query'), ('fan','query'), ('multiColorBox','getInfo')}


def test_partial_multi_box_null_merge():
    first = report('multiColorBox', {'multi_color_box': [
        {'id': 0, 'model_id': 40001, 'temp': 29, 'slots': [{'index': 0, 'type': 'PLA', 'color': [1,2,3]}]},
        {'id': 1, 'model_id': 40002, 'slots': [{'index': 0, 'type': 'PETG'}]}]})
    merged = report('multiColorBox', {'multi_color_box': [{'id': 0, 'temp': None, 'humidity': 33,
        'slots': [{'index': 0, 'type': None, 'color': None}, {'index': 1, 'type': ''}]}]}, first)
    slots = filament_slots(merged['boxes'])
    assert merged['boxes'][0]['temp'] == 29
    assert [(s['slot_number'],s['material']) for s in slots] == [(1,'PLA'), (2,'EMPTY'), (5,'PETG')]
    assert slots[0]['color_hex'] == '010203'
    assert slots[-1]['box_model'] == 'ACE 2'
    assert first['boxes'][0].get('humidity') is None


@pytest.mark.parametrize('wire,expected', [('free','IDLE'), ('preheating','HEATING'), ('auto_leveling','LEVELING'), ('printing','PRINTING'), ('stoped','STOPPING'), ('finished','FINISHED'), ('new-firmware-state','IDLE')])
def test_status_mapping(wire, expected):
    assert report('info', {'project': {'state': wire}})['internal_state'] == expected


@pytest.mark.parametrize('pause,state', [(0,'PRINTING'), (1,'PAUSED'), (2,'PAUSED'), (3,'PRINTING'), (4,'STOPPING')])
def test_pause_authority(pause,state):
    assert report('info', {'project': {'state': 'paused', 'pause': pause}})['internal_state'] == state


def test_push_and_minutes():
    state = report('tempature', {'curr_hotbed_temp': 61})
    state = report('print', {'progress': 25, 'remain_time': 2, 'print_time': 3}, state)
    assert state['bed_temp'] == 61 and state['time_remaining'] == 120 and state['current_ticks'] == 180
    assert report('print', {'state': 'printing'}, state) == state
    assert report('status', None, state) == state


def test_secrets_not_logged_or_retained(monkeypatch, caplog):
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    printer = AnycubicLANPrinter('192.0.2.5')
    values = ['synthetic-token-value', 'synthetic-upload-token', 'synthetic-user', 'synthetic-pass', 'synthetic-device', 'synthetic-cert', 'synthetic-key']
    monkeypatch.setattr(printer, 'handshake', lambda: (_ for _ in ()).throw(ValueError(' '.join(values))))
    with caplog.at_level(logging.DEBUG):
        assert not printer.connect()
    assert all(v not in caplog.text for v in values)
    state = report('info', {'token': values[0], 'urls': {'fileUploadurl': '?s='+values[1]}, 'username': values[2], 'password': values[3], 'deviceId': values[4], 'devicecrt': values[5], 'devicepk': values[6], 'state':'free'})
    assert all(v not in json.dumps(state) for v in values)


from tests.test_contracts.test_cert_A_harness import (  # noqa: E402,F401
    review_db, fernet_env, bind_engine, cert_client, bearer,
)


def test_real_schema_replay_spool_assignment_and_restart(review_db, fernet_env, cert_client, monkeypatch):
    from sqlalchemy import text
    from modules.printers.monitors.anycubic_lan_monitor import AnycubicLANMonitorThread
    from modules.printers.monitors import elegoo_monitor
    engine = review_db.get_bind()
    bind_engine(monkeypatch, engine)
    monkeypatch.setattr(elegoo_monitor, '_mqtt_republish', None)
    pushed = []
    monkeypatch.setattr(elegoo_monitor, 'ws_push', lambda *args: pushed.append(args))
    review_db.execute(text("INSERT INTO printers (id,name,api_type,api_host,slot_count,is_active,org_id) VALUES (499,'Fixture S1','anycubic_lan','192.0.2.49',4,1,1)"))
    review_db.execute(text("INSERT INTO filament_library (id,brand,name,material) VALUES (499,'Fixture','PLA','PLA')"))
    review_db.execute(text("INSERT INTO spools (id,filament_id,initial_weight_g,remaining_weight_g,org_id) VALUES (499,499,1000,500,1)"))
    review_db.commit()

    def replay():
        monitor = AnycubicLANMonitorThread(499, 'Fixture S1', '192.0.2.49')
        client = monitor.client
        client._client = object()
        client._identity = ('20025', '<deviceId>')
        for line in (FIXTURES / 'smoke-40s/messages.jsonl').read_text().splitlines():
            msg = json.loads(line)
            if msg['dir'] == 'in':
                client._on_message(client._client, None, SimpleNamespace(topic=msg['topic'], payload=json.dumps(msg['payload']).encode()))
        return monitor

    first = replay()
    slots = review_db.execute(text('SELECT id,slot_number,filament_type,color_hex FROM filament_slots WHERE printer_id=499 ORDER BY slot_number')).all()
    assert len(slots) == 4 and slots[0][2:] == ('PLA','FF6A14')
    response = cert_client.patch('/printers/499/slots/1',
                                json={'assigned_spool_id':499}, headers=bearer('admin','admin'))
    assert response.status_code == 200, response.text
    second = replay()
    assigned = review_db.execute(text('SELECT id,assigned_spool_id FROM filament_slots WHERE printer_id=499 AND slot_number=1')).one()
    assert assigned == (slots[0][0], 499)
    assert review_db.execute(text('SELECT api_key FROM printers WHERE id=499')).scalar() is None
    assert pushed and review_db.execute(text('SELECT count(*) FROM ams_telemetry WHERE printer_id=499')).scalar() >= 1
    env = review_db.execute(text('SELECT humidity,temperature FROM ams_telemetry WHERE printer_id=499')).all()
    assert env and all(humidity is None and temperature == 29 for humidity, temperature in env)
    first._offline()
    assert review_db.execute(text('SELECT gcode_state FROM printers WHERE id=499')).scalar() == 'OFFLINE'
    # A fresh observation after offline must persist immediately.
    first._process_status(first.client.get_status())
    assert review_db.execute(text('SELECT gcode_state FROM printers WHERE id=499')).scalar() == 'IDLE'
    # Exercise terminal events through the unchanged Elegoo sink as well as parsing.
    import modules.notifications.event_dispatcher as events
    recorded = []
    for event in ('on_print_start', 'on_print_complete', 'on_print_cancelled', 'on_print_failed'):
        monkeypatch.setattr(events, event, lambda *args, _event=event, **kwargs: recorded.append(_event))
    for terminal, event in [('finished', 'on_print_complete'), ('stoped', 'on_print_cancelled')]:
        state = report('info', {'project': {'state':'printing', 'pause':0, 'filename':'fixture.gcode'}})
        first.client._state.update(state)
        first._process_status(first.client.get_status())
        payload = {'state':'free', 'project':None, 'last_project':{'state':terminal, 'pause':1}}
        for _ in range(2):
            state = report('info', payload, state)
            first.client._state.update(state)
            first._process_status(first.client.get_status())
        assert first._last_state == 'IDLE'
        assert recorded.count(event) == 1
    assert 'on_print_failed' not in recorded
    first._retired = True
    second._retired = True


def test_tls_fallback_temp_permissions_refresh_and_retired_callbacks(monkeypatch, tmp_path):
    import os
    import ssl
    import paho.mqtt.client as mqtt
    import core.webhook_utils as network
    printer = AnycubicLANPrinter('192.0.2.49')
    handshakes, clients, cert_paths = [], [], []
    creds = dict(broker='mqtts://192.0.2.49:9883', username='synthetic-user', password='synthetic-pass',
                 deviceId='synthetic-device', devicecrt='synthetic-cert', devicepk='synthetic-key')
    def handshake():
        handshakes.append(True)
        printer.model_id = '20025'
        return creds
    monkeypatch.setattr(printer, 'handshake', handshake)
    monkeypatch.setattr(network, 'device_connect_ip', lambda *a: '192.0.2.49')
    class Context:
        def __init__(self, *a): pass
        def load_cert_chain(self, cert, key):
            for path in (cert, key):
                cert_paths.append(path)
                assert os.stat(path).st_mode & 0o777 == 0o600
    monkeypatch.setattr(ssl, 'SSLContext', Context)
    class Client:
        def __init__(self, *a, **kw):
            clients.append(self)
            assert kw['reconnect_on_failure'] is False
        def username_pw_set(self, *a): pass
        def tls_set_context(self, context): pass
        def connect(self, *a, **kw):
            assert all(not Path(p).exists() for p in cert_paths)
            if len(clients) == 1:
                raise OSError('synthetic TLS failure')
        def loop_start(self): self.on_connect(self,None,None,SimpleNamespace(is_failure=False))
        def subscribe(self, topic): self.subscription = topic
        def disconnect(self): pass
        def loop_stop(self): pass
    monkeypatch.setattr(mqtt, 'Client', Client)
    assert printer.connect() and len(clients) == 2
    old = clients[-1]
    assert printer.connect() and len(handshakes) == 2
    old.on_disconnect(old, None)
    old.on_message(old, None, SimpleNamespace(topic=TOPIC.format('info'), payload=b'{"data":{"state":"free"}}'))
    assert printer._connected and printer._state == {'internal_state': 'OFFLINE'}
    printer.disconnect()
    assert printer._client is None and printer._identity is None


def test_named_secret_and_upload_url_log_redaction(caplog):
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    secrets = {name: f'synthetic-{name}-secret' for name in ('token', 'password', 'deviceId', 'devicecrt', 'devicepk')}
    with caplog.at_level(logging.WARNING):
        logging.getLogger('anycubic-test').warning('%s http://192.0.2.49:18910/gcode_upload?s=synthetic-upload-secret', secrets)
    assert all(v not in caplog.text for v in secrets.values())
    assert 'synthetic-upload-secret' not in caplog.text


def test_controls_fail_closed_without_bambu_fallback(monkeypatch):
    import modules.printers.route_utils as routes
    monkeypatch.setattr(routes, '_bambu_command', lambda *a: pytest.fail('Bambu fallback invoked'))
    for action in ('pause_print', 'resume_print', 'stop_print'):
        assert routes._send_printer_command(SimpleNamespace(api_type='anycubic_lan'), action) is False


def test_invalid_reports_cannot_refresh_liveness():
    printer = AnycubicLANPrinter('192.0.2.49')
    printer._client = object()
    printer._identity = ('20025', 'synthetic-device')
    for data in (None, {}, {'code':500,'data':{'state':'free'}}, {'data':{'urls':{'fileUploadurl':'?s=synthetic'}}}):
        payload = data if isinstance(data,dict) and ('code' in data or 'data' in data) else {'data':data}
        printer._on_message(printer._client,None,SimpleNamespace(topic=TOPIC.format('info'),payload=json.dumps(payload)))
    assert printer.last_info == 0 and printer.last_report == 0


@pytest.mark.parametrize('failure', ['handshake', 'stale-info', 'poll'])
def test_monitor_offline_on_initial_failure_or_stale_lifecycle(monkeypatch, failure):
    import modules.printers.monitors.anycubic_lan_monitor as monitors
    monitor = monitors.AnycubicLANMonitorThread(499, 'Fixture S1', '192.0.2.49')
    now = [100.0]
    monkeypatch.setattr(monitors.time, 'monotonic', lambda: now[0])
    marked = []
    monkeypatch.setattr(monitor, '_offline', lambda: marked.append(True))
    monkeypatch.setattr(monitor, '_discover_and_save_camera', lambda: None)
    # This boundary test needs no DB for the separately tested model write.
    class Engine:
        def begin(self): raise RuntimeError('synthetic database unavailable')
    monkeypatch.setattr(monitors, 'engine', Engine())
    client = monitor.client
    def connect():
        client._connected = failure != 'handshake'
        return client._connected
    monkeypatch.setattr(client,'connect',connect)
    monkeypatch.setattr(client,'poll',lambda: failure != 'poll')
    disconnected = []
    monkeypatch.setattr(client,'disconnect',lambda: disconnected.append(True))
    def wait(_):
        if now[0] > 100 or failure != 'stale-info': monitor._running = False
        now[0] += 100
    monitor._wake = SimpleNamespace(wait=wait)
    monitor.run()
    assert marked
    if failure == 'stale-info': assert disconnected


def test_http_handshake_signing_model_and_pinned_control_destination(monkeypatch):
    import hashlib
    from urllib.parse import urlsplit, parse_qs
    import core.webhook_utils as network
    token = '0123456789ABCDEFfedcba9876543210'
    local = 'synthetic-local'
    creds = {'broker':'mqtts://192.0.2.49:9883','username':'synthetic-user','password':'synthetic-pass',
             'deviceId':'synthetic-device','devicecrt':'synthetic-cert','devicepk':'synthetic-key'}
    pad = PKCS7(128).padder()
    padded = pad.update(json.dumps(creds).encode()) + pad.finalize()
    enc = Cipher(algorithms.AES(token[16:32].encode()), modes.CBC(local.encode().ljust(16,b'\0'))).encryptor()
    encrypted = base64.b64encode(enc.update(padded)+enc.finalize()).decode()
    calls = []
    def request(method, url, **kwargs):
        calls.append((method,url))
        if method == 'GET':
            data = dict(token=token, modelId=20025, ctrlType='lan', ctrlInfoUrl='http://untrusted.invalid:18910/ctrl')
        else:
            parsed = urlsplit(url)
            assert parsed.hostname == '192.0.2.49' and parsed.port == 18910 and parsed.path == '/ctrl'
            query = parse_qs(parsed.query)
            first = hashlib.md5(token[:16].encode()).hexdigest()
            assert query['sign'] == [hashlib.md5((first + query['ts'][0] + query['nonce'][0]).encode()).hexdigest()]
            data = {'code':200,'data':{'token':local,'info':encrypted}}
        return SimpleNamespace(raise_for_status=lambda:None, json=lambda:data)
    monkeypatch.setattr(network,'device_request',request)
    client = AnycubicLANPrinter('192.0.2.49')
    assert client.handshake() == creds
    assert client.model_id == '20025' and len(calls) == 2
    assert client._identity is None and client.get_status().internal_state == 'OFFLINE'


def test_cloud_mode_rejected_before_control_handshake(monkeypatch):
    import core.webhook_utils as network
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'ctrlType':'cloud'})
    monkeypatch.setattr(network,'device_request',request)
    with pytest.raises(ValueError,match='LAN mode required'):
        AnycubicLANPrinter('192.0.2.49').handshake()
    assert len(calls) == 1


@pytest.mark.parametrize('poll_ok', [False, True])
def test_connection_probe_requires_queries_and_returns_no_credentials(monkeypatch, poll_ok):
    import modules.printers.adapters.anycubic_lan as adapter
    import modules.printers.route_utils as routes
    closed = []
    fake = SimpleNamespace(connect=lambda:True, poll=lambda:poll_ok, last_info=1,
                           model_id='20025', disconnect=lambda:closed.append(True),
                           get_status=lambda:SimpleNamespace(internal_state='IDLE', bed_temp=21, nozzle_temp=24, boxes=[]))
    monkeypatch.setattr(adapter, 'AnycubicLANPrinter', lambda host:fake)
    result = routes.test_anycubic_lan_connection('192.0.2.49')
    assert result['success'] is poll_ok and closed == [True]
    if poll_ok: assert result['model'] == 'Kobra S1'
    assert not {'username','password','deviceId','token','devicecrt','devicepk'} & result.keys()

@pytest.mark.parametrize('terminal', ['finished', 'stoped'])
def test_idle_ignores_historical_project(terminal):
    assert report('info', {'state': 'free', 'project': None,
                          'last_project': {'state': terminal, 'pause': 1}})['internal_state'] == 'IDLE'


@pytest.mark.parametrize('terminal,expected', [('finished', 'FINISHED'), ('stoped', 'STOPPING')])
def test_observed_job_terminal_is_one_time(terminal, expected):
    running = report('info', {'project': {'state': 'printing', 'pause': 0}})
    assert running['internal_state'] == 'PRINTING'
    payload = {'state': 'free', 'project': None, 'last_project': {'state': terminal, 'pause': 1}}
    ended = report('info', payload, running)
    assert ended['internal_state'] == expected
    assert report('info', payload, ended)['internal_state'] == 'IDLE'
    assert report('info', dict(payload, state='busy'), ended)['internal_state'] == 'IDLE'


def test_wire_total_layers_and_legacy_fallback():
    assert report('info', {'project': {'state': 'printing', 'total_layers': 123, 'total_layer': 9}})['total_layers'] == 123
    assert report('print', {'progress': 1, 'total_layer': 9})['total_layers'] == 9


def test_busy_and_unknown_preserve_known_state():
    previous = {'internal_state': 'PAUSED'}
    assert report('info', {'state': 'busy', 'project': None}, previous)['internal_state'] == 'PAUSED'
    assert report('info', {'project': {'state': 'new-state'}}, previous)['internal_state'] == 'PAUSED'

@pytest.mark.parametrize('model,humidity,drying,expected', [(40001,0,0,None), (40001,0,1,0), (40002,0,0,0), (40001,35,0,35)])
def test_ace_environment_humidity_policy(monkeypatch, model, humidity, drying, expected):
    import modules.printers.monitors.anycubic_lan_monitor as monitors
    from contextlib import contextmanager
    saved = []
    class Connection:
        def execute(self, statement, params): saved.append(params)
    class Engine:
        @contextmanager
        def begin(self): yield Connection()
    monkeypatch.setattr(monitors, 'engine', Engine())
    monkeypatch.setattr(monitors, 'sync_slots', lambda *args: None)
    monkeypatch.setattr(monitors.ElegooMonitorThread, '_process_status_locked', lambda *args: None)
    monitor = monitors.AnycubicLANMonitorThread(499, 'Fixture S1', '192.0.2.49')
    monitor._process_status_locked(SimpleNamespace(internal_state='IDLE', boxes=[{'id':0, 'model_id':model,
        'temp':29, 'humidity':humidity, 'drying_status':{'status':drying}}]))
    assert saved == [{'pid':499, 'unit':0, 'hum':expected, 'temp':29}]
