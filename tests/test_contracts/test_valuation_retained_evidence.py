"""Historical basis stays tied to immutable lifetime/receipt, not live spool row."""
from copy import deepcopy
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from tests.test_contracts.test_valuation_projection_parser import snapshot
from modules.archives.valuation_retained_evidence import verify_retained_basis


@pytest.fixture
def retained():
    engine=create_engine('sqlite://')
    with Session(engine) as db:
        unit=snapshot()['unit_valuation'];source=unit['sources'][0]
        db.execute(text('CREATE TABLE cost_source_lifetimes(birth_nonce TEXT,resource_kind TEXT,resource_id INTEGER,state TEXT,identity_uid TEXT,owner_scope TEXT,owner_key INTEGER)'))
        db.execute(text('CREATE TABLE cost_identities(identity_uid TEXT,resource_kind TEXT,resource_id INTEGER,owner_scope TEXT,owner_key INTEGER)'))
        db.execute(text('CREATE TABLE spool_purchase_evidence(evidence_nonce TEXT,spool_uid TEXT,birth_nonce TEXT,revision INTEGER,owner_scope TEXT,owner_key INTEGER,currency TEXT,price_decimal TEXT,net_weight_g_decimal TEXT,weight_basis TEXT,provenance TEXT)'))
        db.execute(text("INSERT INTO cost_source_lifetimes VALUES(:birth,'spool',7,'known',:uid,'org',1)"),{'birth':source['birth_nonce'],'uid':source['spool_uid']})
        db.execute(text("INSERT INTO cost_identities VALUES(:uid,'spool',7,'org',1)"),{'uid':source['spool_uid']})
        row=dict(source,owner_scope='org',owner_key=1)
        keys=('evidence_nonce','spool_uid','birth_nonce','revision','owner_scope','owner_key','currency','price_decimal','net_weight_g_decimal','weight_basis','provenance')
        db.execute(text('INSERT INTO spool_purchase_evidence VALUES('+','.join(':'+k for k in keys)+')'),row)
        yield db,unit
    engine.dispose()


def test_deleted_live_spool_needs_no_live_table(retained):
    db,unit=retained
    assert verify_retained_basis(db,unit) is None


@pytest.mark.parametrize('table,field,value',[
    ('cost_source_lifetimes','identity_uid','different'),('cost_source_lifetimes','owner_key',2),
    ('cost_identities','resource_id',8),('spool_purchase_evidence','revision',2),
    ('spool_purchase_evidence','price_decimal','1'),('spool_purchase_evidence','owner_key',2)])
def test_retained_proof_drift_refuses(retained,table,field,value):
    db,unit=retained
    db.execute(text(f'UPDATE {table} SET {field}=:value'),{'value':value})
    with pytest.raises(ValueError,match='Retained valuation'):verify_retained_basis(db,unit)


def test_receipt_deletion_refuses(retained):
    db,unit=retained
    db.execute(text('DELETE FROM spool_purchase_evidence'))
    with pytest.raises(ValueError,match='receipt differs'):verify_retained_basis(db,unit)
