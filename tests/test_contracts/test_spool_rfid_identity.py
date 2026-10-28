"""Owned SQLite/PostgreSQL contracts; printer acquisition is substituted, no credentials."""
import json
import shutil
import subprocess
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from core.schema import bootstrap_database


@pytest.fixture(scope='module')
def postgres_server(tmp_path_factory):
    """An owned trust-auth container. Never discover a configured database/password."""
    if not shutil.which('docker'):
        pytest.skip('Docker unavailable for disposable PostgreSQL')
    config = tmp_path_factory.mktemp('docker-empty-config')
    def docker(*args):
        return subprocess.run(['docker', '--config', str(config), *args], capture_output=True, text=True, timeout=60)
    if docker('info').returncode:
        pytest.skip('Docker unavailable for disposable PostgreSQL')
    name = 'odin-spoolid-test-' + uuid.uuid4().hex[:10]
    started = docker('run', '--pull=never', '-d', '--name', name, '-e', 'POSTGRES_HOST_AUTH_METHOD=trust',
                     '-p', '127.0.0.1::5432', '--tmpfs', '/var/lib/postgresql/data', 'postgres:16-alpine')
    if started.returncode:
        pytest.skip('Local postgres:16-alpine image unavailable; no registry credentials used')
    try:
        for _ in range(100):
            if docker('exec', name, 'pg_isready', '-U', 'postgres').returncode == 0:
                break
            time.sleep(.1)
        port = docker('port', name, '5432/tcp').stdout.strip().rsplit(':', 1)[-1]
        yield docker, name, port
    finally:
        docker('rm', '-f', name)


@pytest.fixture(params=['sqlite', 'postgresql'])
def identity_engine(request, tmp_path, monkeypatch):
    if request.param == 'sqlite':
        engine = create_engine('sqlite:///' + str(tmp_path/'identity.db'))
    else:
        docker, container, port = request.getfixturevalue('postgres_server')
        database = 'spool_' + uuid.uuid4().hex
        assert docker('exec', container, 'createdb', '-U', 'postgres', database).returncode == 0
        for key in ('PGPASSWORD', 'PGSERVICE', 'PGSERVICEFILE', 'DATABASE_PASSWORD_FILE'):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv('PGPASSFILE', '/dev/null')
        engine = create_engine('postgresql+psycopg://postgres@127.0.0.1:' + port + '/' + database)
    bootstrap_database(engine)
    yield engine
    engine.dispose()
    if request.param == 'postgresql':
        docker('exec', container, 'dropdb', '-U', 'postgres', database)


@pytest.fixture
def inventory(identity_engine):
    with identity_engine.begin() as c:
        c.execute(text("INSERT INTO printers(id,name,api_type,api_host,api_key,org_id) VALUES(1,'X1C','bambu','127.0.0.1','fixture',1),(2,'A1','bambu','127.0.0.1','fixture',1),(3,'Foreign','bambu','127.0.0.1','fixture',2)"))
        c.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(1,'Bambu Lab','PLA Matte','PLA'),(2,'Other','PLA Matte','PLA'),(3,'Bambu Lab','PLA Basic','PLA'),(4,'Bambu Lab','PLA Matte','PETG')"))
        c.execute(text("UPDATE printers SET model='X1C',machine_type='X1C' WHERE id=1"))
        c.execute(text("UPDATE printers SET model='A1',machine_type='A1' WHERE id=2"))
    return identity_engine


def seed(engine, spool_id=56, color='Nardo Gray', org=1, filament=1, sealed=True, tag=None, tray_uuid=None, location=None):
    from modules.inventory.receiving import seal_key
    with engine.begin() as c:
        c.execute(text('''INSERT INTO spools(id,filament_id,color_name,org_id,initial_weight_g,remaining_weight_g,
            rfid_tag,tray_uuid,location_printer_id,location_slot,status) VALUES(:id,:filament,:color,:org,1000,1000,:tag,:uuid,:location,:slot,'active')'''),
            dict(id=spool_id, filament=filament, color=color, org=org, tag=tag, uuid=tray_uuid, location=location, slot=1 if location else None))
        if sealed:
            c.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'), dict(key=seal_key(spool_id), value=json.dumps(True)))


def report(**changes):
    return {'id':'0', 'tray_type':'PLA', 'tray_sub_brands':'PLA Matte', 'tray_color':'9F9F9FFF',
            'tag_uid':'1111111111111111', 'tray_uuid':'a'*32, 'tray_id_name':'A01-D0', 'remain':73, **changes}


@pytest.fixture(params=['api', 'monitor'])
def sync(request, inventory, monkeypatch):
    from modules.printers import routes_ams
    from modules.printers.telemetry.bambu import session, broker_policy
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    from modules.printers.monitors.ams_inventory import sync_ams_inventory
    from core.db_utils import _ConnectionAdapter
    monkeypatch.setattr(routes_ams, 'ensure_local_filaments', lambda _: None)
    monkeypatch.setattr(routes_ams, 'get_spoolman_url', lambda _: None)
    monkeypatch.setattr(routes_ams.crypto, 'decrypt', lambda _: 'fixture|fixture')
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: True)
    monkeypatch.setattr(broker_policy, 'resolve_bambu_broker_config', lambda *a, **kw: SimpleNamespace(host='127.0.0.1', port=1883, use_tls=False))
    user = dict(id=1, role='admin', group_id=1, org_id=1, is_superadmin=False)
    def send(tray=None, printer=1, trays=None):
        units = [{'id':'0', 'tray':trays if trays is not None else [tray or report()]}]
        if request.param == 'monitor':
            raw = inventory.raw_connection()
            conn = _ConnectionAdapter(raw, inventory.dialect.name == 'postgresql')
            try:
                result = sync_ams_inventory(conn, printer, units)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise
            finally:
                raw.close()
        section = BambuPrintSection.model_validate({'ams':{'ams':units}})
        monkeypatch.setattr(session, 'read_status_once', lambda *a, **kw: SimpleNamespace(success=True, section=section))
        with Session(inventory) as db:
            return routes_ams.sync_ams_state(printer, current_user=user, db=db)
    send.path = request.param
    return send


def rows(engine, sql):
    with engine.connect() as c:
        return c.execute(text(sql)).all()


def test_bind_lowest_id_clear_seal_audit_and_keep_other(inventory, sync):
    seed(inventory, 57)
    seed(inventory, 56, color='nArDo gRaY')
    sync()
    assert rows(inventory, 'SELECT id,tray_uuid,color_hex,remaining_weight_g,location_printer_id,location_slot FROM spools ORDER BY id') == [(56,'a'*32,'9F9F9F',730,1,1),(57,None,None,1000,None,None)]
    assert rows(inventory, 'SELECT key FROM system_config') == [('inventory.sealed.57',)]
    assert rows(inventory, 'SELECT assigned_spool_id FROM filament_slots') == [(56,)]
    event = rows(inventory, "SELECT action,details FROM audit_logs WHERE action='inventory.rfid_bound'")[0]
    detail = json.loads(event[1]) if isinstance(event[1], str) else event[1]
    assert detail == dict(spool_id=56, tray_uuid='a'*32, printer_id=1, slot=1, candidates=2)


def test_unknown_creates_and_repeats_once(inventory, sync):
    sync()
    sync()
    assert rows(inventory, 'SELECT tray_uuid,rfid_tag,remaining_weight_g FROM spools') == [('a'*32,'1111111111111111',730)]
    assert rows(inventory, "SELECT action FROM audit_logs WHERE action LIKE 'inventory.rfid_%'") == [('inventory.rfid_created',)]


def test_uuid_moves_to_other_printer_with_second_tag(inventory, sync):
    seed(inventory)
    sync()
    sync(report(tag_uid='2222222222222222', remain=25), printer=2)
    assert rows(inventory, 'SELECT id,location_printer_id,location_slot,remaining_weight_g FROM spools') == [(56,2,1,730)]
    assert rows(inventory, 'SELECT printer_id,assigned_spool_id FROM filament_slots ORDER BY printer_id') == [(1,None),(2,56)]


def test_tag_fallback_backfills_and_second_tag_resolves(inventory, sync):
    seed(inventory, sealed=False, tag='1111111111111111')
    sync()
    sync(report(tag_uid='2222222222222222', remain=50))
    assert rows(inventory, 'SELECT id,tray_uuid,remaining_weight_g FROM spools') == [(56,'a'*32,500)]


@pytest.mark.parametrize('tag,uuid_value', [('', ''), ('0'*16, '0'*32), ('   ', None), (None,'000')])
def test_blank_zero_never_bind_or_create(inventory, sync, tag, uuid_value):
    seed(inventory)
    sync(report(tag_uid=tag, tray_uuid=uuid_value))
    assert rows(inventory, 'SELECT id,tray_uuid,rfid_tag,remaining_weight_g FROM spools') == [(56,None,None,1000)]
    assert rows(inventory, "SELECT action FROM audit_logs WHERE action LIKE 'inventory.rfid_%'") == []
    assert rows(inventory, 'SELECT key FROM system_config') == [('inventory.sealed.56',)]


@pytest.mark.parametrize('changes', [dict(org=2), dict(tag='other'), dict(tray_uuid='b'*32), dict(location=2), dict(sealed=False), dict(filament=2), dict(filament=3), dict(filament=4)])
def test_ineligible_sealed_records_never_bound(inventory, sync, changes):
    seed(inventory, **changes)
    sync()
    assert rows(inventory, 'SELECT count(*) FROM spools') == [(2,)]
    assert rows(inventory, "SELECT entity_id FROM audit_logs WHERE action='inventory.rfid_created'")[0][0] != 56


def test_slot_association_excludes_stale_seal(inventory, sync):
    seed(inventory)
    with inventory.begin() as c:
        c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,assigned_spool_id) VALUES(2,1,'PLA',56)"))
    sync()
    assert rows(inventory, 'SELECT count(*) FROM spools') == [(2,)]
    assert rows(inventory, 'SELECT key FROM system_config') == [('inventory.sealed.56',)]


def test_color_fallback_one_color_and_ambiguity_audit(inventory, sync):
    seed(inventory, 56)
    seed(inventory, 57)
    sync(report(tray_id_name='unknown'))
    assert rows(inventory, 'SELECT id FROM spools WHERE tray_uuid IS NOT NULL') == [(56,)]
    seed(inventory, 58, color='Ivory White')
    sync(report(tray_id_name='unknown', tray_uuid='b'*32, tag_uid='2222222222222222'))
    created = rows(inventory, "SELECT details FROM audit_logs WHERE action='inventory.rfid_created'")[0][0]
    detail = json.loads(created) if isinstance(created, str) else created
    assert detail['possible_duplicate'] is True
    assert detail['possible_duplicate_spool_ids'] == [57,58]


def test_known_catalog_color_mismatch_does_not_fallback(inventory, sync):
    seed(inventory, color='Ivory White')
    sync()
    assert rows(inventory, 'SELECT count(*) FROM spools') == [(2,)]


@pytest.mark.parametrize('remain', [None,-1,101,True,12.5,'bad'])
def test_invalid_remaining_preserves_deductions(inventory, sync, remain):
    seed(inventory, sealed=False, tag='1111111111111111')
    with inventory.begin() as c:
        c.execute(text('UPDATE spools SET remaining_weight_g=412 WHERE id=56'))
    if sync.path == 'api' and (isinstance(remain, (bool, float, str))):
        # The V2 parser rejects malformed measurements before route acquisition.
        # The raw daemon accepts the report but omits the invalid measurement.
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            sync(report(remain=remain))
    else:
        sync(report(remain=remain))
    assert rows(inventory, 'SELECT remaining_weight_g FROM spools') == [(412,)]


def test_valid_zero_overrides_retained_weight(inventory, sync):
    seed(inventory)
    sync(report(remain=0))
    assert rows(inventory, 'SELECT remaining_weight_g FROM spools') == [(0,)]


def test_foreign_known_uuid_refuses_entire_batch(inventory, sync):
    seed(inventory, org=2, tray_uuid='a'*32, sealed=False)
    with pytest.raises(Exception):
        sync()
    assert rows(inventory, 'SELECT location_printer_id,remaining_weight_g FROM spools') == [(None,1000)]
    assert rows(inventory, 'SELECT count(*) FROM filament_slots') == [(0,)]


def test_checklist_keeps_bound_created_after_unload_and_scopes_org(inventory, sync):
    from modules.inventory.routes.rfid_onboarding import rfid_onboarding
    seed(inventory)
    sync()
    sync(report(tag_uid='2222222222222222', tray_uuid='b'*32, tray_id_name='A01-W3'))
    sync(report(tray_type=''))
    with Session(inventory) as db:
        result = rfid_onboarding(current_user=dict(id=1, role='operator', group_id=1, org_id=1), db=db)
        assert [r['outcome'] for r in result] == ['bound', 'created']
        assert all(r['checked'] for r in result)
        assert rfid_onboarding(current_user=dict(id=2, role='operator', group_id=2, org_id=2), db=db) == []


@pytest.mark.parametrize('tag,uuid_value', [('0'*16,'a'*32), ('1111111111111111','0'*32)])
def test_either_nonzero_identity_is_sufficient(inventory, sync, tag, uuid_value):
    seed(inventory)
    sync(report(tag_uid=tag, tray_uuid=uuid_value))
    assert rows(inventory, 'SELECT id FROM spools') == [(56,)]
    assert rows(inventory, 'SELECT assigned_spool_id FROM filament_slots') == [(56,)]


def test_uuid_has_priority_over_tag(inventory, sync):
    seed(inventory, 56, tray_uuid='a'*32, sealed=False)
    seed(inventory, 57, tag='1111111111111111', sealed=False)
    sync()
    assert rows(inventory, 'SELECT assigned_spool_id FROM filament_slots') == [(56,)]
    assert rows(inventory, 'SELECT remaining_weight_g FROM spools WHERE id=57') == [(1000,)]


def test_duplicate_uuid_in_batch_is_atomic_refusal(inventory, sync):
    seed(inventory)
    with pytest.raises(Exception):
        sync(trays=[report(), report(id='1', tag_uid='2222222222222222')])
    assert rows(inventory, 'SELECT count(*) FROM filament_slots') == [(0,)]
    assert rows(inventory, 'SELECT tray_uuid FROM spools') == [(None,)]


def test_failed_audit_rolls_back_binding_and_seal(inventory, sync, monkeypatch):
    from modules.inventory import rfid_identity
    from modules.printers import routes_ams
    seed(inventory)
    original = rfid_identity.log_audit
    def fail(db, action, *args, **kwargs):
        result = original(db, action, *args, **kwargs)
        if action.startswith('inventory.rfid_'):
            raise RuntimeError('fixture audit failure')
        return result
    monkeypatch.setattr(rfid_identity, 'log_audit', fail)
    monkeypatch.setattr(routes_ams, 'log_audit', fail)
    with pytest.raises(RuntimeError, match='fixture audit failure'):
        sync()
    assert rows(inventory, 'SELECT tray_uuid,remaining_weight_g FROM spools') == [(None,1000)]
    assert rows(inventory, 'SELECT key FROM system_config') == [('inventory.sealed.56',)]
    assert rows(inventory, 'SELECT count(*) FROM audit_logs') == [(0,)]


def test_case_insensitive_sku_and_canonical_material(inventory, sync):
    seed(inventory)
    sync(report(tray_id_name='a01-d0', tray_type='pla'))
    assert rows(inventory, 'SELECT id,tray_uuid FROM spools') == [(56,'a'*32)]


def test_checklist_http_route_and_requested_org_cannot_cross(inventory, sync):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.db import get_db
    from modules.inventory.routes.rfid_onboarding import router
    seed(inventory)
    sync()
    app = FastAPI()
    app.include_router(router)
    route = next(r for r in app.routes if r.path == '/spools/rfid-onboarding')
    principal = next(dep.call for dep in route.dependant.dependencies if dep.name == 'current_user')
    app.dependency_overrides[principal] = lambda: dict(id=1, role='operator', group_id=2)
    def database():
        with Session(inventory) as db:
            yield db
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        assert client.get('/spools/rfid-onboarding?org_id=1').json() == []
        app.dependency_overrides[principal] = lambda: dict(id=1, role='operator', group_id=1)
        result = client.get('/spools/rfid-onboarding')
        assert result.status_code == 200
        assert result.json()[0]['spool_id'] == 56
        assert result.json()[0]['outcome'] == 'bound'


def test_same_batch_unknown_rolls_consume_distinct_lowest_candidates(inventory, sync):
    seed(inventory,56)
    seed(inventory,57)
    sync(trays=[report(), report(id='1', tag_uid='2222222222222222', tray_uuid='b'*32)])
    assert rows(inventory, 'SELECT id,tray_uuid FROM spools ORDER BY id') == [(56,'a'*32),(57,'b'*32)]
    assert rows(inventory, 'SELECT slot_number,assigned_spool_id FROM filament_slots ORDER BY slot_number') == [(1,56),(2,57)]
    assert rows(inventory, 'SELECT count(*) FROM system_config') == [(0,)]


@pytest.mark.parametrize('model,machine_type', [('X1C', None), ('Bambu Lab X1 Carbon', None),
    ('X1', None), ('X1E', None), ('H2D', None), ('display name', 'H2D'),
    ('display name', 'BL-P001'), ('display name', 'BL-P002'),
    ('display name', 'C13'), ('display name', 'O1D'),
    ('P1S', 'bl-p001'), ('A1', 'bl-p002'), ('P1P', 'c13'), ('A1 mini', 'o1d'),
    ('display name', ' Bambu Lab BL-P001 '), ('display name', ' Bambu O1D ')])
def test_measurements_record_basis(inventory, sync, model, machine_type):
    seed(inventory)
    with inventory.begin() as c:
        c.execute(text('UPDATE printers SET model=:model,machine_type=:machine WHERE id=1'), dict(model=model, machine=machine_type))
    sync(report(remain=80))
    result = rows(inventory, 'SELECT remaining_weight_g,remaining_measured_g,remaining_measured_at,remaining_measured_printer_id FROM spools')[0]
    assert result[0:2] == (800,800)
    assert result[2] is not None and result[3] == 1


@pytest.mark.parametrize('model,machine_type', [('P1S', None), ('P1P', None), ('A1', None),
    ('A1 mini', None), ('unknown', None), (None, None), ('X1C', 'P1S'),
    ('X1C', 'C11'), ('X1C', 'C12'), ('X1C', 'N2S'), ('X1C', 'N1'),
    ('X1C', 'c11'), ('X1C', 'c12'), ('X1C', 'n2s'), ('X1C', 'n1'),
    ('X1C', 'unknown'), ('H2D', 'O1D-unknown')])
def test_nonmeasuring_printers_preserve_basis_and_job_grams(inventory, sync, model, machine_type):
    from core.filament_ledger import record_job_filament
    from core.print_attempts import sqlalchemy_runner
    seed(inventory)
    sync(report(remain=80))
    basis = rows(inventory, 'SELECT remaining_measured_at,remaining_measured_g,remaining_measured_printer_id FROM spools')[0]
    with inventory.begin() as c:
        c.execute(text('UPDATE printers SET model=:model,machine_type=:machine WHERE id=2'), dict(model=model, machine=machine_type))
        c.execute(text("INSERT INTO jobs(id,item_name,status,printer_id) VALUES(700,'50g slicer job','completed',2)"))
    for remain in (-1, 0, 25, 100):
        sync(report(remain=remain), printer=2)
        assert rows(inventory, 'SELECT remaining_weight_g FROM spools') == [(800,)]
    with inventory.begin() as c:
        result = record_job_filament(sqlalchemy_runner(c), 700, source='operator_complete', grams_by_spool={56:50})
        assert result.deductions[0]['grams'] == 50
        assert record_job_filament(sqlalchemy_runner(c), 700, source='operator_complete', grams_by_spool={56:50}).state == 'already_deducted'
    sync(report(remain=100), printer=2)
    assert rows(inventory, 'SELECT remaining_weight_g FROM spools') == [(750,)]
    assert rows(inventory, 'SELECT remaining_measured_at,remaining_measured_g,remaining_measured_printer_id FROM spools')[0] == basis
    assert rows(inventory, 'SELECT job_id,weight_used_g FROM spool_usage') == [(700,50)]
    sync(report(remain=65))
    assert rows(inventory, 'SELECT remaining_weight_g,remaining_measured_g,remaining_measured_printer_id FROM spools') == [(650,650,1)]
    assert rows(inventory, 'SELECT remaining_measured_at FROM spools')[0][0] != basis[0]
    assert rows(inventory, 'SELECT job_id,weight_used_g FROM spool_usage') == [(700,50)]


def test_nonmeasuring_creation_has_no_measurement(inventory, sync):
    sync(report(remain=80), printer=2)
    assert rows(inventory, 'SELECT remaining_weight_g,remaining_measured_g,remaining_measured_at,remaining_measured_printer_id FROM spools') == [(None,None,None,None)]


def test_known_first_sight_is_identified_not_bound(inventory, sync):
    seed(inventory, sealed=False, tag='1111111111111111')
    sync()
    sync()
    assert rows(inventory, "SELECT action FROM audit_logs WHERE action LIKE 'inventory.rfid_%'") == [('inventory.rfid_identified',)]


def test_catalog_only_unambiguous_documented_codes():
    from modules.inventory.rfid_identity import BAMBU_COLORS
    expected = {'A00-Y04':'Gold','A00-Y4':'Gold','A00-D02':'Light Gray','A00-D2':'Light Gray',
        'A00-P01':'Pink','A00-P1':'Pink','A00-A00':'Orange','A00-B08':'Cyan','A00-B8':'Cyan',
        'G02-R0':'Red','G02-D0':'Gray','G02-D1':'Dark Gray','G02-G2':'Forest Green',
        'A19-P00':'Milky Pink','G01-C0':'Clear','A05-M1':'South Beach'}
    assert all(BAMBU_COLORS[sku] == name for sku,name in expected.items())
    assert 'Baby Blue' not in BAMBU_COLORS.values()
    assert all(code not in BAMBU_COLORS for code in ('A00-A0', 'A00-A1', 'A01-A2', 'G01-C00', 'A19-P0'))


def ambiguous_roll(inventory, sync):
    seed(inventory, 56)
    seed(inventory, 57, color='Ivory White')
    sync(report(tray_id_name='unknown', remain=80))
    return rows(inventory, 'SELECT id FROM spools WHERE tray_uuid IS NOT NULL')[0][0]


def merge(inventory, source, target=56):
    from modules.inventory.routes.rfid_onboarding import merge_rfid_roll, MergeRoll
    with Session(inventory) as db:
        return merge_rfid_roll(source, MergeRoll(sealed_spool_id=target),
            current_user=dict(id=1,role='operator',group_id=1), db=db)


def test_merge_moves_identity_basis_and_checklist(inventory, sync):
    from modules.inventory.routes.rfid_onboarding import rfid_onboarding
    source = ambiguous_roll(inventory, sync)
    before = rows(inventory, 'SELECT remaining_measured_at FROM spools WHERE tray_uuid IS NOT NULL')[0][0]
    assert before is not None
    with inventory.begin() as c:
        c.execute(text("""UPDATE spools SET remaining_measured_at='2000-01-01 00:00:00',
            remaining_measured_g=1000,remaining_measured_printer_id=2 WHERE id=56"""))
    with Session(inventory) as db:
        assert rfid_onboarding(current_user=dict(role='operator',group_id=1),db=db)[0]['possible_duplicate_spool_ids'] == [56,57]
    assert merge(inventory, source) == dict(spool_id=56,archived_spool_id=source)
    assert rows(inventory, 'SELECT tray_uuid,rfid_tag,color_hex,location_printer_id,location_slot,remaining_weight_g,remaining_measured_g,remaining_measured_printer_id FROM spools WHERE id=56') == [('a'*32,'1111111111111111','9F9F9F',1,1,800,800,1)]
    assert rows(inventory, 'SELECT remaining_measured_at FROM spools WHERE id=56') == [(before,)]
    assert rows(inventory, "SELECT status,tray_uuid,rfid_tag,location_printer_id,remaining_measured_at FROM spools WHERE status='archived'") == [('archived',None,None,None,None)]
    assert rows(inventory, 'SELECT assigned_spool_id FROM filament_slots') == [(56,)]
    assert rows(inventory, 'SELECT key FROM system_config') == [('inventory.sealed.57',)]
    assert rows(inventory, "SELECT entity_id FROM audit_logs WHERE action='inventory.rfid_merged'") == [(56,)]
    with Session(inventory) as db:
        checklist = rfid_onboarding(current_user=dict(role='operator',group_id=1), db=db)
        assert [(r['spool_id'],r['outcome']) for r in checklist] == [(56,'merged')]
    sync(report(tag_uid='2222222222222222'),printer=2)
    assert rows(inventory, 'SELECT assigned_spool_id FROM filament_slots WHERE printer_id=2') == [(56,)]


@pytest.mark.parametrize('model', ['P1S', 'A1'])
@pytest.mark.parametrize('source_weight', [None, 600])
@pytest.mark.parametrize('target_measured', [False, True])
def test_merge_without_source_measurement_preserves_target_weight_and_basis(
        inventory, sync, model, source_weight, target_measured):
    seed(inventory, 56)
    seed(inventory, 57, color='Ivory White')
    with inventory.begin() as c:
        c.execute(text('UPDATE printers SET model=:model,machine_type=:model WHERE id=2'), {'model':model})
        if target_measured:
            c.execute(text("""UPDATE spools SET remaining_measured_at='2000-01-01 00:00:00',
                remaining_measured_g=1000,remaining_measured_printer_id=1 WHERE id=56"""))
    sync(report(tray_id_name='unknown', remain=80), printer=2)
    source = rows(inventory, 'SELECT id FROM spools WHERE tray_uuid IS NOT NULL')[0][0]
    assert rows(inventory, 'SELECT remaining_weight_g,remaining_measured_at,remaining_measured_g,remaining_measured_printer_id FROM spools WHERE tray_uuid IS NOT NULL') == [(None,None,None,None)]
    with inventory.begin() as c:
        c.execute(text('UPDATE spools SET remaining_weight_g=:weight WHERE id=:id'),
                  {'weight':source_weight, 'id':source})
    before = rows(inventory, 'SELECT remaining_weight_g,remaining_measured_at,remaining_measured_g,remaining_measured_printer_id FROM spools WHERE id=56')
    assert before[0][0] == 1000
    assert merge(inventory, source) == dict(spool_id=56,archived_spool_id=source)
    assert rows(inventory, 'SELECT remaining_weight_g,remaining_measured_at,remaining_measured_g,remaining_measured_printer_id FROM spools WHERE id=56') == before
    assert rows(inventory, 'SELECT tray_uuid,location_printer_id,location_slot FROM spools WHERE id=56') == [('a'*32,2,1)]
    assert rows(inventory, 'SELECT assigned_spool_id FROM filament_slots') == [(56,)]


@pytest.mark.parametrize('change,reason', [
    ("INSERT INTO spool_usage(spool_id,weight_used_g) VALUES(:id,50)", 'usage or deductions'),
    ("INSERT INTO drying_logs(spool_id,duration_hours) VALUES(:id,2)", 'drying history'),
    ("UPDATE spools SET spoolman_spool_id=999 WHERE id=:id", 'Spoolman'),
    ("UPDATE spools SET org_id=2 WHERE id=56", 'Spool not found'),
    ("UPDATE spools SET filament_id=3 WHERE id=56", 'eligible'),
    ("UPDATE spools SET filament_id=4 WHERE id=56", 'eligible'),
    ("UPDATE spools SET status='archived' WHERE id=56", 'eligible'),
    ("DELETE FROM system_config WHERE key='inventory.sealed.56'", 'eligible'),
    ("UPDATE spools SET rfid_tag='another-tag' WHERE id=56", 'eligible'),
    ("UPDATE spools SET location_printer_id=2,location_slot=1 WHERE id=56", 'eligible'),
])
def test_merge_refuses_usage_references_or_ineligible_candidate(inventory, sync, change, reason):
    from fastapi import HTTPException
    source = ambiguous_roll(inventory, sync)
    with inventory.begin() as c:
        c.execute(text(change), {'id':source})
    with pytest.raises(HTTPException) as exc:
        merge(inventory, source)
    assert exc.value.status_code == (404 if reason == 'Spool not found' else 409)
    assert reason in exc.value.detail
    assert rows(inventory, 'SELECT id FROM spools WHERE tray_uuid IS NOT NULL') == [(source,)]
    assert rows(inventory, "SELECT count(*) FROM audit_logs WHERE action='inventory.rfid_merged'") == [(0,)]


def test_merge_requires_possible_duplicate_creation_and_is_atomic(inventory, sync, monkeypatch):
    from fastapi import HTTPException
    from modules.inventory.routes import rfid_onboarding as route_module
    source = ambiguous_roll(inventory, sync)
    # Reject a candidate not named in the creation audit even if newly eligible.
    seed(inventory, 60)
    with pytest.raises(HTTPException) as exc:
        merge(inventory, source, 60)
    assert exc.value.status_code == 409
    def fail(*args, **kwargs):
        raise RuntimeError('audit failed')
    monkeypatch.setattr(route_module, 'log_audit', fail)
    with pytest.raises(RuntimeError, match='audit failed'):
        merge(inventory, source)
    assert rows(inventory, 'SELECT id FROM spools WHERE tray_uuid IS NOT NULL') == [(source,)]
    assert rows(inventory, 'SELECT assigned_spool_id FROM filament_slots') == [(source,)]
    assert ('inventory.sealed.56',) in rows(inventory, 'SELECT key FROM system_config')


def test_merge_refuses_captured_print_before_deduction(inventory, sync):
    from fastapi import HTTPException
    source = ambiguous_roll(inventory, sync)
    with inventory.begin() as c:
        c.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at,spool_snapshot) VALUES(800,1,'running',CURRENT_TIMESTAMP,:snapshot)"),
                  {'snapshot':json.dumps({'slots':[{'spool_id':source}]})})
    with pytest.raises(HTTPException) as exc:
        merge(inventory, source)
    assert exc.value.status_code == 409
    assert 'captured print' in exc.value.detail


@pytest.mark.parametrize('role,auth_kind,scopes,allowed,expected', [
    ('admin',None,None,True,200), ('operator',None,None,True,200),
    ('viewer',None,None,True,403), ('operator',None,None,False,403),
    ('operator','user_api_token',['write:spools'],True,200),
    ('operator','user_api_token',['read:spools'],True,403),
])
def test_merge_http_roles_permissions_and_scope(inventory, sync, monkeypatch, role, auth_kind, scopes, allowed, expected):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.db import get_db
    from core.dependencies import get_current_user
    from core import rbac_policy
    from modules.inventory.routes.rfid_onboarding import router
    source = ambiguous_roll(inventory, sync)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: dict(id=1, role=role, group_id=1,
        _auth_kind=auth_kind, _token_scopes=scopes)
    if not allowed:
        with inventory.begin() as c:
            c.execute(text("INSERT INTO system_config(key,value) VALUES('rbac_permissions',:value)"),
                      {'value':json.dumps({'action_access':{'spools.edit':[]}})})
    def database():
        with Session(inventory) as db:
            yield db
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        result = client.post(f'/spools/rfid-onboarding/{source}/merge', json={'sealed_spool_id':56})
    assert result.status_code == expected, result.text
    if expected == 403:
        assert rows(inventory, 'SELECT id FROM spools WHERE tray_uuid IS NOT NULL') == [(source,)]
