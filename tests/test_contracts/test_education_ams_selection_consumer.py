"""Exact current evidence must match selected physical tray and source material."""
import pytest
from datetime import timedelta
from sqlalchemy import text
from tests.test_contracts.test_ams_evidence_transactions import db,pending,RUNTIME
from modules.printers import services
from modules.printers.telemetry.bambu import ams_evidence_transactions as transactions

@pytest.mark.parametrize('change',['none','material','generation','owner','configuration','expired'])
def test_current_evidence_selection_is_not_cached_inventory(db,pending,monkeypatch,change):
    import core.runtime_identity as identity
    from modules.printers.telemetry.bambu import ams_database_clock
    monkeypatch.setattr(identity,'read_runtime_identity',lambda:RUNTIME)
    monkeypatch.setattr(identity,'revalidate_runtime_identity',lambda value:None)
    transactions._persist_report(db,pending=pending,runtime=RUNTIME);db.commit()
    monkeypatch.setattr(ams_database_clock,'sample_database_clock',lambda db:type('Anchor',(),{
        'database_now':pending.received_at+timedelta(seconds=120 if change=='expired' else 1)})())
    if change=='configuration':db.execute(text("UPDATE printers SET api_host='changed.local'"));db.commit()
    args=dict(printer_id=1,expected_org_id=3 if change=='owner' else 2,
        generation_id='f'*32 if change=='generation' else pending.started.generation_id,
        mapping=[{'source_slot':0,'target_slot':1}],
        verified_payload={'payload_sha256':'d'*64,'storage_bytes':1,'source_index':0,'plate_index':0,'material':'PLA' if change=='material' else 'PETG'})
    if change=='none':assert services.validate_current_ams_selection(db,**args)==[0]
    else:
        with pytest.raises(ValueError,match='refresh required'):services.validate_current_ams_selection(db,**args)
    db.rollback()


@pytest.mark.parametrize('field,value',[('source_index',False),('source_index',0.0),('plate_index',False),
    ('plate_index',0.0),('material','petg'),('material','unknown'),('payload_sha256','invalid'),('storage_bytes',True)])
def test_malformed_staged_facts_cannot_enter_selection(db,pending,field,value):
    facts={'payload_sha256':'d'*64,'storage_bytes':1,'source_index':0,'plate_index':0,'material':'PETG'}
    facts[field]=value
    with pytest.raises(ValueError,match='refresh required'):
        services.validate_current_ams_selection(db,printer_id=1,expected_org_id=2,
            generation_id=pending.started.generation_id,mapping=[{'source_slot':0,'target_slot':1}],verified_payload=facts)
    db.rollback()
