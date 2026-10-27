"""Review regressions: real plate metadata, clean refusal, and worker lifecycle."""
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
import zipfile
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_dispatch_capability import REPORT

EVIDENCE = json.loads((Path(__file__).parents[1] / 'fixtures/h2d-plate-json-evidence.json').read_text())


def evidence_payload(path, file=10, *, model='Bambu Lab H2D', plate=None):
    volume = 'Standard' if file == 10 else 'High Flow'
    settings = {'printer_model': model, 'nozzle_diameter': ['0.4', '0.4'],
        'nozzle_volume_type': [volume, volume], 'physical_extruder_map': ['1', '0'],
        'filament_map': ['2', '1']}
    with zipfile.ZipFile(path, 'w') as package:
        package.writestr('Metadata/project_settings.config', json.dumps(settings))
        package.writestr('Metadata/plate_1.json', json.dumps(plate or EVIDENCE[f'print_file_{file}']))
        package.writestr('Metadata/plate_1.gcode', 'G90\n')
        package.writestr('Metadata/slice_info.config', '<config><plate><metadata key="index" value="1"/>'
            '<filament id="1" used_g="2"/><filament id="2" used_g="12"/></plate></config>')


@pytest.mark.parametrize('file', [10, 11])
def test_real_plate_zero_based_ids_select_left_and_geometry_passes(tmp_path, file):
    from modules.printers.dispatch_capability_payload import inspect_payload, geometry_refusal
    path = tmp_path / 'evidence.3mf'
    evidence_payload(path, file)
    facts = inspect_payload(path, 1, 'H2D')
    assert EVIDENCE[f'print_file_{file}']['bbox_objects_count'] == 150
    assert all('filament_ids' not in obj for obj in facts['objects'])
    assert [r['extruder'] for r in facts['requirements']] == [1]
    assert geometry_refusal(facts, {'model': 'H2D'}) is None
    assert facts['bbox'][0:3:2] == [36.60945, 288.39055]


def test_multiple_nozzles_without_object_assignments_check_only_bed(tmp_path):
    from modules.printers.dispatch_capability_payload import inspect_payload, geometry_refusal
    path = tmp_path / 'both.3mf'
    evidence_payload(path, 11, plate={'bbox_all': [0, 0, 350, 320], 'filament_ids': [0, 1]})
    facts = inspect_payload(path, 1, 'H2D')
    assert geometry_refusal(facts, {'model': 'H2D'}) is None
    assert facts['notes'] == ['Multiple nozzles used; per-nozzle reach unverified without object assignments']
    facts['bbox'][2] = 350.01
    assert 'bed' in geometry_refusal(facts, {'model': 'H2D'})


def setup_h2d(review_db, tmp_path, monkeypatch, file=10, model='Bambu Lab H2D'):
    from modules.printers import dispatch, dispatch_acquisition
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu')
    evidence_payload(path, file, model=model)
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET machine_type='O1D',model='H2D',bed_x_mm=350,bed_y_mm=320 WHERE id=59"))
        conn.execute(text('UPDATE jobs SET payload_sha256=:hash WHERE id=53'), {'hash': hashlib.sha256(path.read_bytes()).hexdigest()})
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a, **kw: None)
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *a: REPORT)
    return engine, path


@pytest.mark.parametrize('file,accepted', [(10, False), (11, True)])
def test_real_evidence_refuses_before_upload_and_releases_claim(review_db, tmp_path, monkeypatch, file, accepted):
    from modules.printers import dispatch
    engine, _ = setup_h2d(review_db, tmp_path, monkeypatch, file)
    calls = []
    monkeypatch.setattr(dispatch, '_dispatch_bambu', lambda *a, **kw: calls.append('upload/START') or (True, 'Print started'))
    success, message = dispatch.dispatch_job(59, 53)
    assert success is accepted, message
    assert calls == (['upload/START'] if accepted else [])
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == int(accepted)
    if not accepted:
        assert 'nozzle_mismatch' in message and 'left nozzle requires Standard; installed High Flow' in message


@pytest.mark.parametrize('start_next_mode', [False, True])
def test_fresh_post_upload_nozzle_change_is_clean_refusal(review_db, tmp_path, monkeypatch, start_next_mode):
    from modules.printers import dispatch, dispatch_acquisition
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu import ftp_upload, session
    engine, _ = setup_h2d(review_db, tmp_path, monkeypatch, 11)
    reports = iter([REPORT, {**REPORT, 'device': {'nozzle': {'info': [{'id': 1, 'type': 'HS01', 'diameter': .4}]}}}])
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *a: next(reports))
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: True)
    calls = []
    monkeypatch.setattr(ftp_upload, 'upload_file', lambda *a: calls.append('upload') or True)
    monkeypatch.setattr(session, 'run_command', lambda *a, **kw: pytest.fail('START must not be reached'))
    if start_next_mode:
        from modules.printers import start_next
        from modules.printers.models import Printer
        with engine.begin() as conn: conn.execute(text("UPDATE jobs SET status='pending' WHERE id=53"))
        monkeypatch.setattr(start_next, 'next_suggestion', lambda *a, **kw: {'job_id': 53, 'fingerprint': 'f'*64})
        data = SimpleNamespace(request_id=uuid.uuid4(), suggested_job_id=53, suggestion_fingerprint='f'*64,
                               bed_clear_confirmed=True, attestation='owner_in_app')
        result = start_next.start(review_db, review_db.get(Printer, 59), {'id': 3, 'role': 'admin', 'group_id': 1}, data)
        assert result['state'] == 'refused', result
        success, message = result['success'], result['message']
    else:
        success, message = dispatch.dispatch_job(59, 53)
    assert not success and 'nozzle_mismatch' in message and 'reconciliation' not in message
    assert calls == ['upload']
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0


@pytest.mark.parametrize('model', ['Bambu Lab A1', 'Bambu Lab P1S'])
def test_model_guard_refuses_before_claim(review_db, tmp_path, monkeypatch, model):
    from core import dispatch_claims
    from modules.printers import dispatch
    setup_h2d(review_db, tmp_path, monkeypatch, 11, model=model)
    monkeypatch.setattr(dispatch_claims, 'acquire', lambda *a, **kw: pytest.fail('Must refuse before claim'))
    success, message = dispatch.dispatch_job(59, 53)
    assert not success and 'printer_model_mismatch' in message


@pytest.mark.parametrize('code', ['hardened_steel', 'stainless_steel', 'HA01', 'HX01', 'HE01', 'HU01', 'HB01'])
def test_flow_only_known_for_s_and_h(code):
    from modules.printers.nozzle_capability import decode_type
    assert decode_type(code)[1] is None


def test_dual_top_level_is_not_physical_zero():
    from modules.printers.nozzle_capability import installed_nozzles
    assert installed_nozzles({'nozzle_type': 'HH05', 'nozzle_diameter': .4}, dual=True) == []


def test_unverified_enqueue_failure_does_not_refuse(tmp_path, monkeypatch):
    from tests.test_contracts.test_dispatch_capability import gate
    from core import job_timeline
    monkeypatch.setattr(job_timeline, 'enqueue', lambda *a: (_ for _ in ()).throw(RuntimeError('queue unavailable')))
    assert gate(tmp_path, monkeypatch, 'High Flow', {})() is True


@pytest.mark.parametrize('failure', ['slots_busy', 'thread_start'])
def test_start_next_staging_failure_restores_assignment_and_guard(token_client, review_db, tmp_path, monkeypatch, failure):
    from modules.printers import start_next, routes_start_next, dispatch_background as background
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='pending',printer_id=NULL WHERE id=53"))
    monkeypatch.setattr(start_next, 'next_suggestion', lambda *a, **kw: {'job_id': 53, 'fingerprint': 'f'*64})
    if failure == 'slots_busy':
        monkeypatch.setattr(background, '_slots', SimpleNamespace(acquire=lambda **kw: False))
    else:
        monkeypatch.setattr(background, 'Thread', lambda **kw: SimpleNamespace(start=lambda: (_ for _ in ()).throw(RuntimeError('cannot start'))))
    client, issue = token_client
    client.app.include_router(routes_start_next.router)
    body = {'suggested_job_id': 53, 'suggestion_fingerprint': 'f'*64, 'request_id': str(uuid.uuid4()), 'bed_clear_confirmed': True}
    headers = issue(['write:jobs'])
    response = client.post('/printers/59/start-next', headers=headers, json=body)
    assert response.status_code == 503, response.text
    with engine.connect() as conn:
        assert conn.execute(text('SELECT printer_id FROM jobs WHERE id=53')).scalar_one() is None
        assert not conn.execute(text("SELECT key FROM system_config WHERE key LIKE 'start_next_claim:%'")).all()
    replay = client.post('/printers/59/start-next', headers=headers, json=body)
    assert replay.json()['state'] == 'refused'


def test_staging_timeout_returns_handle_and_terminal_progress_is_removed(token_client, review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, dispatch_background as background
    from modules.jobs.routes import jobs_lifecycle, dispatch_status
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    client, issue = token_client
    client.app.include_router(jobs_lifecycle.router); client.app.include_router(dispatch_status.router)
    headers = issue(['write:jobs', 'read:jobs'])
    release = threading.Event()
    monkeypatch.setattr(background, 'STAGING_SECONDS', .01)
    def blocked(*a):
        assert release.wait(5)
        return False, 'nozzle_mismatch: left nozzle requires Standard; installed High Flow'
    monkeypatch.setattr(dispatch, 'dispatch_job', blocked)
    try:
        response = client.post('/jobs/53/dispatch', headers=headers)
        assert response.status_code == 202, response.text
        handle = response.json()
        url = handle['status_url'].removeprefix('/api')
        assert client.get(url, headers=headers).json()['phase'] == 'preparing'
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(url, headers=headers).json()
        if result['phase'] == 'refused': break
        time.sleep(.01)
    assert result['phase'] == 'refused' and 'nozzle_mismatch' in result['message']
    with engine.connect() as conn:
        assert not conn.execute(text("SELECT key FROM system_config WHERE key LIKE 'dispatch_progress:%'")).all()


def test_preclaim_validation_stays_400(token_client, review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch
    from modules.jobs.routes import jobs_lifecycle
    configure_dispatch(review_db, tmp_path, monkeypatch)
    client, issue = token_client; client.app.include_router(jobs_lifecycle.router)
    monkeypatch.setattr(dispatch, 'dispatch_job', lambda *a: (False, 'Payload is unavailable'))
    result = client.post('/jobs/53/dispatch', headers=issue(['write:jobs']))
    assert result.status_code == 400 and 'Payload is unavailable' in result.text


def test_shutdown_drain_is_bounded_and_waits_for_workers(monkeypatch):
    from modules.printers import dispatch_background as background
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(5)); worker.start()
    monkeypatch.setattr(background, '_workers', {worker})
    try:
        before = time.monotonic()
        assert background.drain(.02) is False
        assert time.monotonic() - before < .5
        release.set()
        assert background.drain(1) is True
    finally:
        release.set(); worker.join(); background.startup()


@pytest.mark.parametrize('state,phase', [('preparing', 'needs_reconciliation'), ('adapter_entered', 'needs_reconciliation'),
                                       ('accepted', 'start_accepted'), ('uncertain', 'needs_reconciliation')])
def test_claim_state_overrides_stale_uploading_progress(token_client, review_db, tmp_path, monkeypatch, state, phase):
    from modules.printers import dispatch
    from modules.jobs.routes import dispatch_status
    from core.durable_config import insert
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', lambda *a: (True, 'Print started'))
    assert dispatch.dispatch_job(59, 53)[0]
    with engine.begin() as conn:
        token = conn.execute(text('SELECT token FROM printer_dispatch_claims WHERE job_id=53')).scalar_one()
        conn.execute(text('UPDATE printer_dispatch_claims SET state=:state WHERE token=:token'), {'token': token, 'state': state})
        insert(conn, 'dispatch_progress:' + token, {'job_id': 53, 'org_id': 1, 'printer_id': 59,
            'attempt_id': token, 'phase': 'uploading', 'retry_blocked': True, 'observation': None})
    client, issue = token_client; client.app.include_router(dispatch_status.router)
    response = client.get('/jobs/53/dispatch-status?attempt_id=' + token, headers=issue(['read:jobs']))
    assert response.status_code == 200 and response.json()['phase'] == phase


@pytest.mark.parametrize('dual', [True, False])
def test_report_waits_past_status_only_packet_until_nozzle_packet(monkeypatch, dual):
    from modules.printers import dispatch_bambu_readiness as readiness
    calls = []
    class Client:
        def __init__(self, *a, **kw): pass
        def username_pw_set(self, *a): pass
        def connect(self, *a, **kw): pass
        def subscribe(self, *a, **kw): return 0, 1
        def publish(self, *a, **kw): return SimpleNamespace(rc=0)
        def loop_start(self):
            self.on_connect(self, None, None, 0)
            self.on_subscribe(self, None, 1, [0])
            base = {'command': 'push_status', 'gcode_state': 'IDLE', 'print_error': 0}
            if dual:
                base.update(nozzle_type='HS01', nozzle_diameter=.4)
            def send(packet):
                self.on_message(self, None, SimpleNamespace(topic='device/test/report', retain=False,
                    payload=json.dumps({'print': packet}).encode()))
            send(base)
            # The initial packet cannot complete the report read.
            calls.append('status')
            def later():
                time.sleep(.03)
                calls.append('nozzles')
                send({**base, **(REPORT if dual else {'nozzle_type': 'hardened_steel', 'nozzle_diameter': .4})})
            self.worker = threading.Thread(target=later); self.worker.start()
        def disconnect(self): self.on_disconnect(self, None, None, 0)
        def loop_stop(self): self.worker.join()
    monkeypatch.setattr('paho.mqtt.client.Client', Client)
    monkeypatch.setattr('core.itar.enforce_host_destination', lambda *a, **kw: None)
    monkeypatch.setattr('core.webhook_utils.device_connect_ip', lambda *a: '127.0.0.1')
    monkeypatch.setattr('modules.printers.telemetry.bambu.broker_policy.resolve_bambu_broker_config',
        lambda *a: SimpleNamespace(host='127.0.0.1', port=1, use_tls=False))
    result = readiness.fresh_bambu_status_is_idle({'ip': 'test', 'serial': 'test', 'access_code': '',
        'model': 'H2D' if dual else 'P1S'}, time.monotonic() + 1, return_report=True)
    assert calls == ['status', 'nozzles']
    assert isinstance(result, dict)
    assert ('device' if dual else 'nozzle_type') in result


def test_nozzle_then_ams_share_remaining_freshness_budget(tmp_path, monkeypatch):
    from tests.test_contracts.test_dispatch_capability import gate
    from modules.printers import dispatch_capability_gate, dispatch_acquisition
    clock = [100.0]
    monkeypatch.setattr(dispatch_capability_gate.time, 'monotonic', lambda: clock[0])
    deadlines = []
    def ams(): return True
    def prepare(*, deadline):
        deadlines.append(deadline)
        assert deadline - clock[0] == 2
        clock[0] += 1
        return True
    ams.prepare = prepare
    ams.refusal = None
    checked = gate(tmp_path, monkeypatch, 'High Flow', previous=ams)
    def report(*args):
        clock[0] += 8
        return REPORT
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', report)
    assert checked.prepare() and checked()
    assert deadlines == [110.0]


def test_start_next_timeout_replays_same_pollable_handle(token_client, review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, start_next, routes_start_next, dispatch_background as background
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    with engine.begin() as conn: conn.execute(text("UPDATE jobs SET status='pending' WHERE id=53"))
    monkeypatch.setattr(start_next, 'next_suggestion', lambda *a, **kw: {'job_id': 53, 'fingerprint': 'f'*64})
    monkeypatch.setattr(background, 'STAGING_SECONDS', .01)
    release = threading.Event()
    def slow(*a):
        assert release.wait(5)
        return False, 'Nozzle mismatch'
    monkeypatch.setattr(dispatch, 'dispatch_job', slow)
    client, issue = token_client; client.app.include_router(routes_start_next.router)
    headers = issue(['write:jobs'])
    body = {'suggested_job_id': 53, 'suggestion_fingerprint': 'f'*64, 'request_id': str(uuid.uuid4()), 'bed_clear_confirmed': True}
    try:
        first = client.post('/printers/59/start-next', headers=headers, json=body)
        second = client.post('/printers/59/start-next', headers=headers, json=body)
        assert first.status_code == second.status_code == 202
        assert first.json()['attempt_id'] == second.json()['attempt_id']
        assert second.json()['accepted'] and second.json()['status_url']
    finally:
        release.set()
        assert background.drain(5)
        background.startup()


def test_refusal_timeline_records_bounded_structured_fields(monkeypatch):
    from core import job_timeline
    from modules.printers import dispatch
    from modules.printers.dispatch_ams_gate import GateRefusal
    notes = []
    monkeypatch.setattr(job_timeline, 'enqueue', notes.append)
    dispatch._notify_start_outcome({'id': 89, 'printer_id': 5}, 'adapter_rejected',
        gate_refusal=GateRefusal('nozzle_mismatch', 'Untrusted device text ' * 1000))
    assert notes[0]['code'] == 'NOZZLE_MISMATCH'
    assert json.loads(notes[0]['detail']) == {'gate': 'NOZZLE_MISMATCH'}
    with pytest.raises(ValueError, match='detail field'):
        job_timeline.event(89, 'gate_refused', detail={'message': 'Free text is not a timeline field'})
