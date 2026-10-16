"""Actual H2D monitor observation to authenticated nozzle-status readback."""
from types import SimpleNamespace
from sqlalchemy import text
from tests.test_contracts.test_printer_offline_behavior import offline_store, review_db, v2_offline_monitor
from tests.test_contracts.test_api_token_delegation_behavior import token_client
import pytest
from contextlib import contextmanager


def test_distinct_h2d_nozzles_survive_monitor_to_http_readback(v2_offline_monitor, offline_store, token_client):
    from modules.printers import routes_status
    monitor, _, _ = v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET machine_type='H2D' WHERE id=199"));offline_store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','nozzle_temper':210,'nozzle_target_temper':220,'nozzle_temper_1':190,'nozzle_target_temper_1':200}}))
    client,issue=token_client;client.app.include_router(routes_status.router)
    response=client.get('/printers/199/nozzle-status',headers=issue(['read:printers']))
    assert response.status_code==200
    assert response.json()['nozzle_count']==2
    assert response.json()['nozzle_0']['temp']==210
    assert response.json()['nozzle_1']['temp']==190
    assert response.json()['nozzle_1']['target']==200
    assert response.json()['nozzle_1']['temp_observed_at']
    assert response.json()['nozzle_1']['target_observed_at']


@pytest.mark.parametrize('invalid', [None, True, 'invalid', float('nan'), float('inf'), 10**1000], ids=['null','bool','string','nan','infinity','oversized'])
def test_partial_invalid_nozzle_reports_preserve_measurement_and_timestamp(v2_offline_monitor,offline_store,monkeypatch,invalid):
    monitor,_,module=v2_offline_monitor
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','nozzle_temper_1':190,'nozzle_target_temper_1':200}}))
    before=tuple(offline_store.execute(text('SELECT nozzle_1_temp,nozzle_1_temp_observed_at FROM printers WHERE id=199')).one())
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2011))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'nozzle_temper_1':invalid,'nozzle_target_temper_1':0}}))
    assert tuple(offline_store.execute(text('SELECT nozzle_1_temp,nozzle_1_temp_observed_at FROM printers WHERE id=199')).one())==before
    assert offline_store.execute(text('SELECT nozzle_1_target_temp FROM printers WHERE id=199')).scalar_one()==0


def test_right_nozzle_commit_refusal_retries_without_uncommitted_telemetry(v2_offline_monitor,offline_store,monkeypatch):
    monitor,_,module=v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET machine_type='H2D' WHERE id=199"));offline_store.commit()
    original=module.get_db;failed=[False];events=[]
    class Connection:
        def __init__(self,conn):self.conn=conn
        def __getattr__(self,name):return getattr(self.conn,name)
        def commit(self):
            if not failed[0]:
                failed[0]=True
                self.conn.rollback()
                raise RuntimeError('synthetic commit refusal')
            self.conn.commit()
    @contextmanager
    def database():
        with original() as conn:yield Connection(conn)
    monkeypatch.setattr(module,'get_db',database)
    monkeypatch.setattr(module,'ws_push',lambda *a,**kw:events.append((a,kw)))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','nozzle_temper_1':190,'nozzle_target_temper_1':0}}))
    assert tuple(offline_store.execute(text('SELECT nozzle_1_temp,nozzle_1_target_temp FROM printers WHERE id=199')).one())==(None,None)
    assert monitor._pending_right_nozzle
    assert events==[]
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE'}}))
    assert tuple(offline_store.execute(text('SELECT nozzle_1_temp,nozzle_1_target_temp FROM printers WHERE id=199')).one())==(190,0)
    assert monitor._pending_right_nozzle=={}
    assert events


@pytest.mark.parametrize('invalid', [None, True, 'invalid', float('nan'), float('inf'), 10**1000], ids=['null','bool','string','nan','infinity','oversized'])
def test_invalid_raw_right_nozzle_does_not_replace_live_valid_measurement(v2_offline_monitor,offline_store,monkeypatch,invalid):
    monitor,_,module=v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET machine_type='H2D' WHERE id=199"));offline_store.commit()
    events=[]
    monkeypatch.setattr(module,'ws_push',lambda kind,payload:events.append((kind,payload)))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','nozzle_temper_1':190,'nozzle_target_temper_1':200}}))
    assert events[-1][1]['h2d_nozzles']['nozzle_1']['temp']==190
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2011))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'nozzle_temper_1':invalid,'nozzle_target_temper_1':0}}))
    assert events[-1][1]['h2d_nozzles']['nozzle_1']['temp']==190
    assert events[-1][1]['h2d_nozzles']['nozzle_1']['target']==0


@pytest.mark.parametrize('retirement', ['retired', 'old-generation'])
def test_retired_right_nozzle_observation_cannot_change_pending_or_storage(v2_offline_monitor,offline_store,retirement):
    monitor,_,_=v2_offline_monitor
    if retirement=='retired':
        monitor._connection_retired=True
        generation=None
    else:
        generation=monitor._connection_generation-1
    monitor._on_status(SimpleNamespace(raw_data={'print':{'nozzle_temper_1':190}}),generation=generation)
    assert not getattr(monitor,'_pending_right_nozzle',{})
    assert 'nozzle_temper_1' not in monitor._state
    assert offline_store.execute(text('SELECT nozzle_1_temp FROM printers WHERE id=199')).scalar_one() is None


def test_throttled_right_measurement_keeps_observation_timestamp_until_flush(v2_offline_monitor,offline_store,monkeypatch):
    monitor,_,module=v2_offline_monitor
    events=[]
    monkeypatch.setattr(module,'ws_push',lambda kind,payload:events.append((kind,payload)))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE'}}))
    events.clear()
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2001))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'nozzle_temper_1':0}}))
    observed=monitor._pending_right_nozzle['nozzle_1_temp'][2]
    assert events==[]
    assert offline_store.execute(text('SELECT nozzle_1_temp FROM printers WHERE id=199')).scalar_one() is None
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2011))
    monitor._on_status(SimpleNamespace(raw_data={'print':{}}))
    row=offline_store.execute(text('SELECT nozzle_1_temp,nozzle_1_temp_observed_at FROM printers WHERE id=199')).one()
    assert row[0]==0
    assert row[1]==observed.isoformat()
    assert not monitor._pending_right_nozzle
    assert events


@pytest.mark.parametrize('invalid', [True, 'invalid', float('nan'), float('inf'), 10**1000], ids=['bool','string','nan','infinity','oversized'])
def test_h2d_parser_rejects_invalid_measurements_without_inventing_zero(invalid):
    from modules.printers.monitors.mqtt_telemetry import parse_h2d_nozzles
    assert parse_h2d_nozzles({'nozzle_temper_1':invalid}) is None
    assert parse_h2d_nozzles({'nozzle_temper':invalid,'nozzle_temper_1':0})=={
        'nozzle_0':{'temp':None,'target':None},'nozzle_1':{'temp':0,'target':None}}


@pytest.mark.parametrize('reported_type',['O1D','Bambu Lab H2D','H2D'])
def test_h2d_autodetection_persists_capability_before_authenticated_readback(v2_offline_monitor,offline_store,token_client,reported_type):
    from modules.printers import routes_status
    monitor,_,_=v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET machine_type=NULL, model='Owner selected model' WHERE id=199"));offline_store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print':{
        'gcode_state':'IDLE','printer_type':reported_type,'nozzle_temper_1':190}}))
    assert tuple(offline_store.execute(text('SELECT machine_type,model FROM printers WHERE id=199')).one())==('H2D','Owner selected model')
    client,issue=token_client;client.app.include_router(routes_status.router)
    response=client.get('/printers/199/nozzle-status',headers=issue(['read:printers']))
    assert response.status_code==200
    assert response.json()['nozzle_count']==2
    assert response.json()['nozzle_1']['temp']==190


def _captured_info(name):
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / 'fixtures' / 'telemetry' / name
    for line in path.read_text().splitlines():
        payload = json.loads(line).get('payload') or {}
        if isinstance(payload.get('info'), dict) and payload['info'].get('module'):
            return payload['info']
    raise AssertionError('no get_version reply in fixture')


@pytest.mark.parametrize('fixture,expected', [('bambu-h2d-failure-arc.jsonl', 'H2D'), ('bambu-a1-happy-path.jsonl', 'A1')])
def test_v2_monitor_detects_model_from_captured_get_version_reply(v2_offline_monitor, offline_store, fixture, expected):
    from modules.printers.telemetry.bambu.raw import BambuInfoSection, BambuPrintSection
    from modules.printers.telemetry.events import BambuInfoEvent, BambuReportEvent
    monitor, emitters, _ = v2_offline_monitor
    offline_store.execute(text("UPDATE printers SET machine_type=NULL, model=NULL WHERE id=199")); offline_store.commit()
    emitters[0](BambuInfoEvent('monitor-199', 2000, BambuInfoSection.model_validate(_captured_info(fixture))))
    emitters[0](BambuReportEvent('monitor-199', 2000, BambuPrintSection.model_validate({'gcode_state': 'IDLE'}), mqtt_retained=False))
    offline_store.expire_all()
    assert tuple(offline_store.execute(text('SELECT machine_type,model FROM printers WHERE id=199')).one()) == (expected, expected)


def test_v2_monitor_requests_get_version_on_connect(v2_offline_monitor):
    from modules.printers.telemetry.events import ConnectionEvent
    monitor, emitters, _ = v2_offline_monitor
    requested = []
    monitor._v2_adapter.request_version = lambda: requested.append(True) or True
    emitters[0](ConnectionEvent('monitor-199', 2000, 'connected'))
    assert requested == [True]


def test_v2_adapter_request_version_publishes_get_version():
    import json
    from types import SimpleNamespace
    from modules.printers.telemetry.bambu.adapter import BambuTelemetryAdapter, BambuAdapterConfig
    adapter = BambuTelemetryAdapter(BambuAdapterConfig('synthetic', 'SYNTHSERIAL', '127.0.0.1', ''), emitter=lambda e: None)
    published = []
    adapter._client = SimpleNamespace(publish=lambda topic, payload, qos=0: published.append((topic, json.loads(payload))) or SimpleNamespace(rc=0))
    assert adapter.request_version() is False  # not connected yet
    adapter._connected = True
    assert adapter.request_version() is True
    assert published == [('device/SYNTHSERIAL/request', {'info': {'sequence_id': '0', 'command': 'get_version'}})]


@pytest.mark.parametrize('machine_type,count', [('H2D', 2), ('H2D Pro', 2), ('H2C', 2), ('O1E', 2), ('O1C2', 2),
                                                ('H2S', 1), ('X1C', 1), ('P2S', 1), (None, 1)])
def test_nozzle_status_api_uses_model_nozzle_capability(v2_offline_monitor, offline_store, token_client, machine_type, count):
    from modules.printers import routes_status
    offline_store.execute(text("UPDATE printers SET machine_type=:m, nozzle_1_temp=190 WHERE id=199"), {'m': machine_type}); offline_store.commit()
    client, issue = token_client; client.app.include_router(routes_status.router)
    body = client.get('/printers/199/nozzle-status', headers=issue(['read:printers'])).json()
    assert body['nozzle_count'] == count
    assert (body['nozzle_1'] is None) == (count == 1)


@pytest.mark.parametrize('machine_type', ['H2D Pro', 'H2C'])
def test_dual_nozzle_models_get_dual_nozzle_websocket_parsing(v2_offline_monitor, offline_store, monkeypatch, machine_type):
    monitor, _, module = v2_offline_monitor
    pushed = []
    monkeypatch.setattr(module, 'ws_push', lambda kind, payload: pushed.append(payload))
    offline_store.execute(text("UPDATE printers SET machine_type=:m WHERE id=199"), {'m': machine_type}); offline_store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state': 'IDLE', 'nozzle_temper': 210, 'nozzle_temper_1': 190}}))
    assert pushed and pushed[-1]['h2d_nozzles']['nozzle_1']['temp'] == 190


def test_bambu_nozzle_counts_match_installed_bambu_studio_profiles():
    import json
    from pathlib import Path
    from modules.printers.printer_models import BAMBU_MODEL_IDS, bambu_nozzle_count
    machine = Path('/Applications/BambuStudio.app/Contents/Resources/profiles/BBL/machine')
    if not machine.is_dir():
        pytest.skip('Bambu Studio profiles not installed')
    profiles = {}
    for path in machine.glob('*.json'):
        try:
            data = json.loads(path.read_text())
        except ValueError:
            continue
        profiles[data.get('name')] = data
    def resolve(name, key):
        data = profiles.get(name)
        while data is not None:
            if key in data:
                return data[key]
            data = profiles.get(data.get('inherits'))
    for data in [d for d in profiles.values() if 'model_id' in d and d['model_id'] in BAMBU_MODEL_IDS]:
        variants = [n for n, v in profiles.items() if v.get('printer_model') == data['name']]
        counts = {len(resolve(v, 'nozzle_diameter') or []) for v in variants}
        assert counts == {bambu_nozzle_count(data['model_id'])}, data['name']
