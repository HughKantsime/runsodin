"""The Education read endpoint cannot persist the six in the frozen 020 schema."""
import time
import pytest
from types import SimpleNamespace
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db
from tests.test_contracts.test_six_materials import SIX

@pytest.mark.parametrize('material', SIX)
def test_six_teacher_read_refuses_durable_evidence(token_client,review_db,monkeypatch,material):
    from modules.organizations import education_access,routes_education_reviews
    from modules.organizations import education_ams_read_service as service
    from modules.printers.telemetry.bambu import ams_evidence_transactions as transactions,ams_database_clock
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
    monkeypatch.setattr(education_access,'require_education_license',lambda:None)
    key=Fernet.generate_key();monkeypatch.setenv('ENCRYPTION_KEY',key.decode())
    review_db.execute(text('UPDATE printers SET api_host=:host,api_key=:credential WHERE id=9'),
        {'host':'printer.local','credential':Fernet(key).encrypt(b'SYNTHETIC|test-only-access').decode()})
    review_db.commit()
    runtime=SimpleNamespace(incarnation='a'*32,clock_domain='synthetic-domain')
    import core.runtime_identity as runtime_identity
    monkeypatch.setattr(runtime_identity,'read_runtime_identity',lambda:runtime)
    monkeypatch.setattr(transactions,'revalidate_runtime_identity',lambda value:None)
    monkeypatch.setattr(ams_database_clock,'revalidate_runtime_identity',lambda value:None)
    active=[]
    class OwnedSession(Session):
        def __enter__(self):active.append(self);return super().__enter__()
        def __exit__(self,*args):
            try:return super().__exit__(*args)
            finally:active.remove(self)
    monkeypatch.setattr(service,'SessionLocal',lambda:OwnedSession(review_db.get_bind()))
    observed=[]
    def network(config,**kwargs):
        assert not active,'Owned DB session must close before network'
        observed.append(kwargs['operation_id'])
        snapshot=RawAMSSnapshot(kwargs['operation_id'],'b'*32,1,time.monotonic(),kwargs['configuration_key'],
            kwargs['owner_org_id'],(RawTrayFact(1,0,0,material,'reported'),))
        return RuntimeBoundAMSResult(True,snapshot,runtime.incarnation,runtime.clock_domain,True,None)
    from modules.printers.telemetry.bambu import trusted_ams_acquisition
    monkeypatch.setattr(trusted_ams_acquisition,'read_current_selected_ams',network)
    client,issue=token_client;client.app.include_router(routes_education_reviews.router)
    headers=issue(['write:education'])
    response=client.post('/education/submissions/14/ams-read',headers=headers,
        json={'revision':1,'printer_id':9,'selected_slots':[1]})
    assert not active
    assert len(observed) == 1
    assert response.status_code == 409, response.text
    assert 'Selected AMS trays could not be verified' in response.text
    with Session(review_db.get_bind()) as fresh:
        row=fresh.execute(text('SELECT state FROM ams_read_generations')).scalar_one()
        assert row == 'failed'
        assert fresh.execute(text('SELECT count(*) FROM ams_report_packets')).scalar_one()==0
        assert fresh.execute(text('SELECT count(*) FROM ams_current_reads')).scalar_one()==0



from tests.test_contracts.test_ams_evidence_transactions import db, pending, RUNTIME


@pytest.mark.parametrize('material', SIX)
def test_durable_writer_rejects_six_before_inserting_packet(db, pending, material):
    from dataclasses import replace
    from modules.printers.telemetry.bambu import ams_evidence_transactions as transactions
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    from core.ams_evidence_schema import SCHEMA_MATERIALS, packets, trays
    assert material not in SCHEMA_MATERIALS
    changed = replace(pending, facts=(RawTrayFact(1,0,0,material,'reported'),))
    with pytest.raises(transactions.AMSTransitionUnavailable, match='AMS report persistence unavailable'):
        transactions._persist_report(db, pending=changed, runtime=RUNTIME)
    db.rollback()
    assert db.execute(packets.select()).first() is None
    assert db.execute(trays.select()).first() is None


from tests.test_contracts.test_ams_evidence_schema import connection, generation, packet


@pytest.mark.parametrize('material', SIX)
def test_frozen_database_check_also_refuses_six(connection, material):
    from sqlalchemy.exc import IntegrityError
    from core.ams_evidence_schema import trays
    generation(connection)
    sequence = packet(connection)
    with pytest.raises(IntegrityError):
        connection.execute(trays.insert().values(packet_sequence=sequence,slot_number=1,
            unit_id=0,tray_id=0,material=material,material_state='reported'))
    assert connection.execute(trays.select()).first() is None
