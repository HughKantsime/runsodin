"""Actual JWT archive reprint prospective birth; no physical dispatch."""
import pytest
from modules.printers.nozzle_capability import installed_nozzles
from sqlalchemy import text
from tests.test_contracts.test_archive_reprint_behavior import reprint_client
from tests.test_contracts.test_education_review_workflow import review_db


def test_archive_reprint_has_real_actor_owned_fresh_identity(reprint_client,review_db):
    from core.dependencies import get_current_user
    from core.auth import create_access_token
    client,_,_=reprint_client
    client.app.dependency_overrides.pop(get_current_user)
    response=client.post("/archives/101/reprint",headers={"Authorization":"Bearer "+create_access_token({"sub":"admin"})},json={"printer_id":9})
    assert response.status_code==200,response.text
    identifier=response.json()["job_id"]
    review_db.expire_all()
    row=review_db.execute(text("SELECT j.economic_uid,c.owner_scope,c.owner_key,c.resource_kind,c.resource_id FROM jobs j LEFT JOIN cost_identities c ON c.identity_uid=j.economic_uid WHERE j.id=:id"),{"id":identifier}).one()
    assert row[0]
    assert tuple(row[1:])==("org",1,"job",identifier)



def test_archive_reprint_registry_fault_rolls_back_job_and_registry(reprint_client,review_db):
    from core.dependencies import get_current_user
    from core.auth import create_access_token
    client,_,_=reprint_client
    client.app.dependency_overrides.pop(get_current_user)
    def counts():
        return tuple(review_db.execute(text("SELECT count(*) FROM "+table)).scalar_one() for table in ("jobs","cost_identities"))
    before=counts();review_db.commit()
    review_db.execute(text("CREATE TRIGGER owned_fail_reprint_registry BEFORE INSERT ON cost_identities WHEN NEW.resource_kind='job' BEGIN SELECT RAISE(ABORT, 'owned reprint registry failure'); END"));review_db.commit()
    response=client.post("/archives/101/reprint",headers={"Authorization":"Bearer "+create_access_token({"sub":"admin"})},json={"printer_id":9})
    assert response.status_code==500
    review_db.expire_all()
    assert counts()==before


@pytest.mark.parametrize("path",["legacy","v2"])
def test_unqualified_single_plate_ams_source_refuses_before_claim_or_transport(reprint_client,review_db,monkeypatch,path):
    from core import db as core_db
    from modules.printers import dispatch
    from modules.printers.telemetry import feature_flag
    client,_,_=reprint_client
    review_db.execute(text("UPDATE print_files SET filaments_json='[{\"slot\": 1,\"type\":\"PLA\"}]' WHERE id=101"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,4,'PLA')"));review_db.commit()
    response=client.post("/archives/101/reprint",json={"printer_id":9,"plate_index":0,"ams_mapping":[{"source_slot":0,"target_slot":4}]})
    assert response.status_code==200,response.text
    identifier=response.json()["job_id"]
    monkeypatch.setattr(core_db,"engine",review_db.get_bind())
    monkeypatch.setattr(feature_flag,"is_v2_enabled",lambda:path=="v2")
    monkeypatch.setattr(dispatch,"_dispatch_bambu",lambda *a,**k:pytest.fail("Unqualified source touched transport"))
    ok,reason=dispatch.dispatch_job(9,identifier)
    assert not ok and "AMS source or plate is unsupported" in reason
    assert review_db.execute(text("SELECT count(*) FROM printer_dispatch_claims")).scalar_one()==0
    assert review_db.execute(text("SELECT status FROM jobs WHERE id=:id"),{"id":identifier}).scalar_one()=="pending"


@pytest.mark.parametrize('path', ['legacy', 'v2'])
@pytest.mark.parametrize('failure', [None, 'material', 'busy', 'acquisition', 'configuration', 'source',
    'expired_during_connect', 'claim_during_connect', 'configuration_during_connect'])
def test_ungrouped_archive_reprint_explicit_source_zero_to_tray_one(
        reprint_client, review_db, monkeypatch, path, failure):
    """Normal reprint API -> real admission/gate -> instrumented transport, no hardware."""
    import hashlib, time
    from pathlib import Path
    from types import SimpleNamespace
    from core import db as core_db
    from modules.printers import dispatch, dispatch_ams_gate as gate, dispatch_acquisition
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot, RawTrayFact
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
    from tests.test_contracts.test_ams_evidence_transactions import RUNTIME
    from tests.test_contracts.test_ams_source_inspection import archive
    client, actor, source = reprint_client
    actor.update(role='admin', group_id=None)
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.execute(text("INSERT INTO printers(id,name,api_type,api_host,api_key,org_id,is_active,gcode_state) SELECT 109,'Ungrouped owner printer',api_type,api_host,api_key,NULL,1,'IDLE' FROM printers WHERE id=9"))
    from core import crypto
    import secrets
    review_db.execute(text('UPDATE printers SET api_key=:key WHERE id=109'),
        {'key':crypto.encrypt('SYNTHETICPRINTER|' + secrets.token_urlsafe(24))})
    review_db.execute(text('UPDATE models SET org_id=NULL WHERE id=101'))
    review_db.execute(text('UPDATE jobs SET charged_to_org_id=NULL WHERE id=101'))
    review_db.execute(text("UPDATE print_files SET org_id=NULL,filaments_json='[{\"slot\": 1,\"type\":\"PETG\"}]' WHERE id=101"))
    review_db.execute(text('UPDATE print_archives SET org_id=NULL WHERE id=101'))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(109,1,'PETG')"))
    review_db.commit()
    payload = archive(settings='{"filament_type":["PETG","PLA","PLA"]}',
        code='; filament_type = PETG;PLA;PLA\nM620 S0A\nM621 S0A\nT0\n')
    source.write_bytes(payload)
    original = tuple(review_db.execute(text('SELECT status,model_id,printer_id FROM jobs WHERE id=101')).one())
    review_db.rollback()
    response = client.post('/archives/101/reprint', json={'printer_id':109, 'plate_index':0,
        'ams_mapping':[{'source_slot':0,'target_slot':1}]})
    assert response.status_code == 200, response.text
    identifier = response.json()['job_id']
    row = review_db.execute(text('SELECT ams_mapping,payload_sha256,charged_to_org_id FROM jobs WHERE id=:id'), {'id':identifier}).one()
    import json
    assert json.loads(row[0]) == [0] and row[1] == hashlib.sha256(payload).hexdigest() and row[2] is None
    review_db.rollback()
    monkeypatch.setattr(core_db, 'engine', review_db.get_bind())
    # This contract instruments all physical boundaries; absent nozzle facts
    # exercise the permitted, explicitly unverified capability outcome.
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *args: {})
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda:path=='v2')
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda:RUNTIME)
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', lambda _:None)
    monkeypatch.setattr(dispatch, '_ws', lambda *a:None)
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *a:None)
    events = []
    clock = [time.monotonic()]
    monkeypatch.setattr(gate, 'time', SimpleNamespace(monotonic=lambda:clock[0]))
    def acquired(config, **options):
        events.append('fresh_read')
        assert options['selected_slots'] == (1,) and options['owner_org_id'] is None
        assert 'upload' in events
        if failure == 'configuration':
            with review_db.get_bind().begin() as conn:
                conn.execute(text("UPDATE printers SET api_host='changed.example.test' WHERE id=109"))
        snapshot = RawAMSSnapshot(options['operation_id'], 'c'*32, 1, clock[0],
            options['configuration_key'], None,
            (RawTrayFact(1,0,0,'PLA' if failure=='material' else 'PETG','reported'),), failure!='busy', nozzles=tuple(installed_nozzles(
                {'nozzle_type': 'hardened_steel', 'nozzle_diameter': .4})))
        return RuntimeBoundAMSResult(failure!='acquisition', snapshot, RUNTIME.incarnation,
            RUNTIME.clock_domain, True, None)
    monkeypatch.setattr(gate, 'read_current_selected_ams', acquired)
    def uploaded(staged, remote):
        events.append('upload')
        assert Path(staged).read_bytes() == payload
        return True
    def connected():
        assert events == ['upload', 'fresh_read']
        events.append('connect')
        if failure == 'expired_during_connect': clock[0] += 11
        if failure in ('claim_during_connect', 'configuration_during_connect'):
            with review_db.get_bind().begin() as conn:
                conn.execute(text("UPDATE printer_dispatch_claims SET state='uncertain'" if failure == 'claim_during_connect' else "UPDATE printers SET api_host='changed.example.test' WHERE id=109"))
        return True
    def started(*args, **options):
        events.append('start')
        assert options['ams_mapping'] == [0]
        return True
    if path == 'legacy':
        from modules.printers.adapters import bambu
        monkeypatch.setattr(bambu, 'BambuPrinter', lambda **kwargs:SimpleNamespace(
            upload_file=uploaded, connect=connected, start_print=started,
            disconnect=lambda:events.append('cleanup')))
    else:
        from modules.printers.telemetry.bambu import ftp_upload, session
        monkeypatch.setattr(ftp_upload,'upload_file',lambda host,code,staged,remote:uploaded(staged,remote))
        monkeypatch.setattr(session,'BambuCommandAdapter',lambda config:SimpleNamespace(
            start=connected, is_connected=lambda:True, start_print=started, stop=lambda:None))
    if failure == 'source': source.write_bytes(b'changed source')
    ok, reason = dispatch.dispatch_job(109, identifier)
    assert ok is (failure is None), reason
    assert events.count('start') == (1 if failure is None else 0)
    assert tuple(review_db.execute(text('SELECT status,model_id,printer_id FROM jobs WHERE id=101')).one()) == original
    assert review_db.execute(text('SELECT job_id FROM print_archives WHERE id=101')).scalar_one() == 101
