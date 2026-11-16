"""Bambu measurements use the installation's tenancy mode at first start."""
from contextlib import contextmanager
import json

import pytest
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch


@pytest.fixture
def mqtt_case(review_db, tmp_path, monkeypatch):
    from core.db_utils import _ConnectionAdapter
    from modules.archives import archive
    from modules.notifications import alert_dispatch, event_dispatcher
    from modules.printers.monitors import mqtt_job_lifecycle

    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu')
    with engine.begin() as conn:
        # Ordinary groups may exist without any organizations.
        conn.execute(text('UPDATE groups SET is_org=FALSE'))
        conn.execute(text('UPDATE printers SET org_id=NULL WHERE id=59'))
        conn.execute(text('UPDATE models SET org_id=NULL WHERE id=51'))
        conn.execute(text('UPDATE print_files SET org_id=NULL,filament_weight_grams=120,filaments_json=:materials WHERE id=52'),
                     {'materials': json.dumps([{'slot': 1, 'type': 'PLA', 'used_grams': 120}])})
        conn.execute(text("UPDATE jobs SET charged_to_org_id=NULL,ams_mapping='[0]' WHERE id IN (53,54)"))
        conn.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(700,'Synthetic','PLA')"))
        conn.execute(text('INSERT INTO spools(id,filament_id,initial_weight_g,remaining_weight_g,org_id) VALUES(700,700,1000,800,NULL),(701,700,1000,800,NULL)'))
        conn.execute(text('UPDATE filament_slots SET assigned_spool_id=700 WHERE printer_id=59'))

    @contextmanager
    def database():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()

    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', database)
    monkeypatch.setattr(archive, 'get_db', database)
    for name in ('dispatch_alert', 'record_error', 'increment_care_counters'):
        monkeypatch.setattr(event_dispatcher, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda *args, **kwargs: None)
    monkeypatch.setattr(alert_dispatch, '_start_bed_cooled_monitor', lambda *args, **kwargs: None)
    return engine, database


@pytest.mark.parametrize('has_org,printer_org,spool_org,shared,accepted', [
    (False, None, None, False, True),
    (True, None, None, False, False),
    (True, None, None, True, False),
    (False, None, 1, False, False),
    (False, 1, None, False, False),
    (True, 1, 1, False, True),
])
def test_capture_respects_install_tenancy(mqtt_case, has_org, printer_org, spool_org, shared, accepted):
    from modules.archives.services import capture_attempt_spools
    from modules.printers.monitors.mqtt_job_lifecycle import capture_mqtt_start_weights

    _, database = mqtt_case
    with database() as conn:
        conn.execute('UPDATE groups SET is_org=? WHERE id=1', (has_org,))
        conn.execute('UPDATE printers SET org_id=?,shared=? WHERE id=59', (printer_org, shared))
        conn.execute('UPDATE spools SET org_id=? WHERE id=700', (spool_org,))
        conn.execute("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(700,59,'Synthetic','running',CURRENT_TIMESTAMP)")
        assert capture_attempt_spools(conn, 700, 59, source='synthetic-first-start')
        weights = capture_mqtt_start_weights(conn, 700, 59, {'_ams_remain': {1: 80}})
        assert weights == ({700: {'remain_pct': 80, 'initial_weight_g': 1000, 'slot': 1}} if accepted else {})
        snapshot = json.loads(conn.execute('SELECT spool_snapshot FROM print_jobs WHERE id=700').fetchone()[0])
        assert snapshot['mqtt_start_weights'] == ({str(k): v for k, v in weights.items()})


@pytest.mark.parametrize('change', ['organization_created', 'spool_owner_changed', 'reassigned'])
def test_capture_rechecks_live_tenancy_and_identity(mqtt_case, change):
    from modules.archives.services import capture_attempt_spools
    from modules.printers.monitors.mqtt_job_lifecycle import capture_mqtt_start_weights

    _, database = mqtt_case
    with database() as conn:
        conn.execute("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(700,59,'Synthetic','running',CURRENT_TIMESTAMP)")
        assert capture_attempt_spools(conn, 700, 59, source='synthetic-first-start')
        statement = {
            'organization_created': 'UPDATE groups SET is_org=TRUE WHERE id=1',
            'spool_owner_changed': 'UPDATE spools SET org_id=1 WHERE id=700',
            'reassigned': 'UPDATE filament_slots SET assigned_spool_id=701 WHERE printer_id=59',
        }[change]
        conn.execute(statement)
        assert capture_mqtt_start_weights(conn, 700, 59, {'_ams_remain': {1: 80}}) == {}


@pytest.mark.parametrize('status,end_pct,grams,provenance', [
    ('completed', 70, 100, 'measured'),
    ('failed', 70, 100, 'measured'),
    ('cancelled', 70, 100, 'measured'),
    ('failed', 80, None, None),
    ('completed', 80, 120, 'sliced_estimate'),
])
def test_single_tenant_monitor_accounts_measured_usage(mqtt_case, monkeypatch, status, end_pct, grams, provenance):
    from core import auth, dispatch_claims
    from core.dispatch_identity import build_identity
    from modules.printers import dispatch
    from modules.printers.dispatch_admission import admit
    from modules.printers.monitors import mqtt_job_lifecycle as lifecycle

    engine, _ = mqtt_case
    # Admit an owned synthetic attempt without entering printer transport.
    job = dispatch._load_job(53)
    creds = dispatch._get_printer_info(59)
    key = auth.SECRET_KEY.encode()
    identity = build_identity(creds, key)
    claim = dispatch_claims.acquire(engine, 59, 53, identity.target_key,
                                    identity.configuration_key, job['payload_sha256'], None)
    assert claim.status == 'acquired'
    filename = f'odin-dispatch-{claim.token}.3mf'
    admitted = admit(engine, token=claim.token, job=job, creds=creds, key=key,
                     payload_sha256=job['payload_sha256'], transport_filename=filename,
                     power_accepted=True)
    assert admitted.admitted, admitted.reason
    state = {'subtask_name': filename, 'gcode_file': 'Metadata/plate_1.gcode', '_ams_remain': {1: 80}}
    identifier, linked, weights = lifecycle.record_job_started(59, 'Synthetic', state, lambda **kw: None, lambda: None)
    assert identifier is not None and linked == 53
    assert weights == {700: {'remain_pct': 80, 'initial_weight_g': 1000, 'slot': 1}}

    def no_recapture(*args, **kwargs):
        raise AssertionError('Resume must reuse first-start evidence')

    monkeypatch.setattr(lifecycle, 'capture_mqtt_start_weights', no_recapture)
    assert lifecycle.record_job_started(59, 'Synthetic', dict(state, _ams_remain={1: 75}), lambda **kw: None, lambda: None) == (identifier, linked, weights)
    for _ in range(2):
        lifecycle.record_job_ended(59, 'Synthetic', identifier, linked, status,
                                   dict(state, _ams_remain={1: end_pct}), weights, lambda **kw: None)

    with engine.connect() as conn:
        archive = conn.execute(text('SELECT accounting_state,accounting_reason,consumption_json FROM print_archives WHERE print_job_id=:id'), {'id': identifier}).one()
        balance = conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=700')).scalar_one()
        ledger = conn.execute(text('SELECT spool_id,weight_used_g FROM spool_usage WHERE print_job_id=:id'), {'id': identifier}).all()
        measured = conn.execute(text('SELECT filament_used_g FROM print_jobs WHERE id=:id'), {'id': identifier}).scalar_one()
    assert measured == (100 if end_pct == 70 else None)
    if grams is None:
        assert archive[:2] == ('unresolved', 'non_successful_attempt_quantity_unconfirmed')
        assert balance == 800 and ledger == []
    else:
        assert archive[0] == 'accounted'
        assert archive[1] != 'non_successful_attempt_quantity_unconfirmed'
        consumption = json.loads(archive[2])
        assert [(item['spool_id'], item['grams_used'], item['quantity_provenance']) for item in consumption] == [(700, grams, provenance)]
        assert balance == 800 - grams
        assert ledger == [(700, grams)]
