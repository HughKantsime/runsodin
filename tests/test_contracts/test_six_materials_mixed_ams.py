"""Mixed reports retain the trial/2.3.0 durable outcomes for every selection."""
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db
from tests.test_contracts.test_six_materials import SIX


@pytest.mark.parametrize('material', SIX)
@pytest.mark.parametrize('baseline', [True, False], ids=['trial230-vocabulary', 'stage1'])
@pytest.mark.parametrize('selected', [[1], [2], [1, 2]], ids=['PLA', 'six', 'both'])
def test_mixed_report_durable_outcome(token_client, review_db, monkeypatch, material, baseline, selected):
    from core import material_identity, runtime_identity
    from core.ams_evidence_schema import SCHEMA_MATERIALS
    from modules.organizations import education_access, routes_education_reviews, education_ams_read_service
    from modules.printers.telemetry.bambu import ams_evidence_transactions, ams_database_clock, trusted_ams_acquisition
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSAcquisitionCollector
    from modules.printers.telemetry.bambu.ams_evidence import parse_raw_ams_facts
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult

    # Parser, collector and persistence bytes are unchanged from trial/2.3.0.
    # Its identity vocabulary is precisely the frozen durable schema vocabulary.
    if baseline:
        monkeypatch.setattr(material_identity, '_MATERIALS', set(SCHEMA_MATERIALS))
    packet = {'ams': {'ams': [{'id': '0', 'tray': [
        {'id': '0', 'tray_type': 'PLA'}, {'id': '1', 'tray_type': material}]}]}}
    facts = parse_raw_ams_facts(packet)
    assert (facts[1].material, facts[1].material_state) == ((None, 'unknown') if baseline else (material, 'reported'))
    monkeypatch.setattr(education_access, 'require_education_license', lambda: None)
    key = Fernet.generate_key()
    monkeypatch.setenv('ENCRYPTION_KEY', key.decode())
    review_db.execute(text('UPDATE printers SET api_host=:host,api_key=:credential WHERE id=9'),
        {'host': 'printer.local', 'credential': Fernet(key).encrypt(b'SYNTHETIC|test-only-access').decode()})
    review_db.commit()
    runtime = SimpleNamespace(incarnation='a'*32, clock_domain='synthetic-domain')
    monkeypatch.setattr(runtime_identity, 'read_runtime_identity', lambda: runtime)
    monkeypatch.setattr(runtime_identity, 'revalidate_runtime_identity', lambda _: None)
    monkeypatch.setattr(ams_evidence_transactions, 'revalidate_runtime_identity', lambda _: None)
    monkeypatch.setattr(ams_database_clock, 'revalidate_runtime_identity', lambda _: None)
    monkeypatch.setattr(education_ams_read_service, 'SessionLocal', lambda: Session(review_db.get_bind()))

    def network(config, **kwargs):
        collector = RawAMSAcquisitionCollector(topic='device/fixture/report',
            configuration_key=kwargs['configuration_key'], owner_org_id=kwargs['owner_org_id'],
            selected_slots=kwargs['selected_slots'], subscribe_mid=1,
            incarnation=kwargs['operation_id'], allow_unreported=True)
        assert collector.acknowledge_subscription(collector.incarnation, 1, [0])
        assert collector.observe(incarnation=collector.incarnation, topic='device/fixture/report', retained=False, packet=packet)
        snapshot = collector.snapshot(configuration_key=kwargs['configuration_key'], owner_org_id=kwargs['owner_org_id'])
        assert [f.slot_number for f in snapshot.trays] == selected
        return RuntimeBoundAMSResult(True, snapshot, runtime.incarnation, runtime.clock_domain, True, None)

    monkeypatch.setattr(trusted_ams_acquisition, 'read_current_selected_ams', network)
    client, issue = token_client
    client.app.include_router(routes_education_reviews.router)
    response = client.post('/education/submissions/14/ams-read', headers=issue(['write:education']),
        json={'revision': 1, 'printer_id': 9, 'selected_slots': selected})
    accepted = selected == [1]
    assert response.status_code == (200 if accepted else 409), response.text
    if not accepted:
        assert 'Selected AMS trays could not be verified' in response.text
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT state FROM ams_read_generations')).scalar_one() == ('ready' if accepted else 'failed')
        assert fresh.execute(text('SELECT material,material_state FROM ams_report_trays')).all() == ([('PLA', 'reported')] if accepted else [])
        assert fresh.execute(text('SELECT count(*) FROM ams_report_packets')).scalar_one() == int(accepted)
        assert fresh.execute(text('SELECT count(*) FROM ams_current_reads')).scalar_one() == int(accepted)
        if accepted:
            from modules.printers.services import validate_current_ams_selection
            assert validate_current_ams_selection(fresh, printer_id=9, expected_org_id=1,
                generation_id=response.json()['generation_id'], mapping=[{'source_slot': 0, 'target_slot': 1}],
                verified_payload=dict(payload_sha256='d'*64, storage_bytes=1, source_index=0,
                    plate_index=0, material='PLA')) == [0]
