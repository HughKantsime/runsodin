"""Executable constraints of review-only schema, not application migration proof."""
from pathlib import Path
import sqlite3
import pytest

@pytest.fixture
def contract():
    conn=sqlite3.connect(':memory:')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript((Path(__file__).parents[2]/'artifacts/application-certification/cost-identity-contract.sql').read_text())
    for uid,kind,owner in [('order-a','order',1),('item-a','item',1),('schedule-a','schedule',1),
                           ('attempt-a','attempt',1),('job-a','job',1),('spool-a','spool',1),('order-b','order',2)]:
        conn.execute('INSERT INTO cost_identities VALUES(?,?,?,?,?)',(uid,kind,1,'org',owner))
    for uid,kind in [('item-a','item'),('schedule-a','schedule'),('job-a','job')]:
        conn.execute('INSERT INTO cost_order_memberships VALUES(?,?,?,?,?,?)',(uid,kind,'order-a','order','org',1))
    conn.execute("INSERT INTO cost_job_bindings VALUES('job-a','job','order-a','item-a','item','org',1)")
    conn.execute("INSERT INTO cost_attempt_bindings VALUES('attempt-a','attempt','job-a','order-a','item-a','org',1)")
    yield conn
    conn.close()


def obligation(conn,order='order-a',item='item-a',source='schedule-a'):
    conn.execute("INSERT INTO cost_obligations(obligation_uid,order_uid,item_uid,owner_scope,owner_key,source_uid,allocation_key,obligation_kind,required_quantity,evidence_state) VALUES('obligation-a',?,?,'org',1,?,'printed-model-a','printed_pieces','2','recorded')",(order,item,source))

@pytest.mark.parametrize('scope,key',[('org',0),('org',-1),('global',1),('global',-1)])
def test_scope_key_relationship_is_enforced(contract,scope,key):
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute('INSERT INTO cost_identities VALUES(?,?,?,?,?)',('invalid','order',9,scope,key))

@pytest.mark.parametrize('order,item,source',[('spool-a','item-a','schedule-a'),('order-b','item-a','schedule-a'),('order-a','spool-a','schedule-a'),('order-a','item-a','attempt-a')])
def test_obligations_reject_wrong_identity_kind_or_owner(contract,order,item,source):
    with pytest.raises(sqlite3.IntegrityError):obligation(contract,order,item,source)


def test_obligation_blocks_registry_deletion_and_retains_missing_attempt_evidence(contract):
    obligation(contract)
    contract.execute("INSERT INTO cost_attempt_participation VALUES('attempt-a','attempt','obligation-a','org',1,'started',NULL)")
    with pytest.raises(sqlite3.IntegrityError):contract.execute("DELETE FROM cost_identities WHERE identity_uid='order-a'")
    assert contract.execute('SELECT required_quantity FROM cost_obligations').fetchone()==('2',)
    assert contract.execute('SELECT state FROM cost_attempt_participation').fetchone()==('started',)


@pytest.mark.parametrize('uid,kind,scope,key',[('spool-a','attempt','org',1),('attempt-a','attempt','org',2)])
def test_attempt_participation_rejects_wrong_kind_and_scope(contract,uid,kind,scope,key):
    obligation(contract)
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute('INSERT INTO cost_attempt_participation VALUES(?,?,?,?,?,?,?)',
                         (uid,kind,'obligation-a',scope,key,'started',None))


def test_unreferenced_identity_cannot_be_deleted_and_reused(contract):
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute("DELETE FROM cost_identities WHERE identity_uid='spool-a'")


def test_obligation_evidence_cannot_be_silently_rewritten(contract):
    obligation(contract)
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute("UPDATE cost_obligations SET required_quantity='999' WHERE obligation_uid='obligation-a'")


def test_recorded_obligation_rejects_invalid_quantity_text(contract):
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute("INSERT INTO cost_obligations(obligation_uid,order_uid,item_uid,owner_scope,owner_key,source_uid,allocation_key,obligation_kind,required_quantity,evidence_state) VALUES('bad','order-a','item-a','org',1,'schedule-a','bad','printed_pieces','not-a-quantity','recorded')")


def test_one_attempt_can_participate_in_multiple_obligations(contract):
    obligation(contract)
    contract.execute("INSERT INTO cost_obligations SELECT 'obligation-b',order_uid,order_kind,item_uid,item_kind,owner_scope,owner_key,source_uid,source_kind,'printed-model-b',obligation_kind,required_quantity,evidence_state,evidence_reason FROM cost_obligations WHERE obligation_uid='obligation-a'")
    for uid in ['obligation-a','obligation-b']:
        contract.execute('INSERT INTO cost_attempt_participation VALUES(?,?,?,?,?,?,?)',
                         ('attempt-a','attempt',uid,'org',1,'started',None))
    assert contract.execute('SELECT COUNT(*) FROM cost_attempt_participation').fetchone()==(2,)


def test_same_org_order_mismatch_is_rejected(contract):
    contract.execute("INSERT INTO cost_identities VALUES('other-order','order',2,'org',1)")
    with pytest.raises(sqlite3.IntegrityError):obligation(contract,order='other-order')


@pytest.mark.parametrize('kind,quantity',[
    ('printed_pieces','0'),('printed_pieces','01'),('printed_pieces','1.5'),
    ('printed_pieces','-1'),('printed_pieces','1e2'),('printed_pieces','9'*19),
    ('purchased_units','0'),('purchased_units','00.5'),('purchased_units','.5'),
    ('purchased_units','1.'),('purchased_units','1.50'),('purchased_units','1.2.3'),
    ('purchased_units','NaN'),('purchased_units','0.'+'1'*13),
    ('explicit_no_production','1'),
])
def test_noncanonical_or_invalid_obligation_quantity_is_rejected(contract,kind,quantity):
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute("INSERT INTO cost_obligations(obligation_uid,order_uid,item_uid,owner_scope,owner_key,source_uid,allocation_key,obligation_kind,required_quantity,evidence_state) VALUES('quantity','order-a','item-a','org',1,'schedule-a','quantity',?,?, 'recorded')",(kind,quantity))


@pytest.mark.parametrize('kind,quantity',[('printed_pieces','1'),('purchased_units','0.125'),('purchased_units','12.99'),('purchased_units','1000'),('explicit_no_production','0')])
def test_canonical_obligation_quantity_is_preserved_exactly(contract,kind,quantity):
    contract.execute("INSERT INTO cost_obligations(obligation_uid,order_uid,item_uid,owner_scope,owner_key,source_uid,allocation_key,obligation_kind,required_quantity,evidence_state) VALUES('quantity','order-a','item-a','org',1,'schedule-a','quantity',?,?, 'recorded')",(kind,quantity))
    assert contract.execute("SELECT required_quantity FROM cost_obligations WHERE obligation_uid='quantity'").fetchone()==(quantity,)


@pytest.mark.parametrize('state,reason',[('recorded','invented'),('unresolved',None),('unresolved',''),('unresolved','   ')])
def test_evidence_state_and_reason_must_agree(contract,state,reason):
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute("INSERT INTO cost_obligations(obligation_uid,order_uid,item_uid,owner_scope,owner_key,source_uid,allocation_key,obligation_kind,required_quantity,evidence_state,evidence_reason) VALUES('reason','order-a','item-a','org',1,'schedule-a','reason','printed_pieces','1',?,?)",(state,reason))


def test_global_scope_is_valid_without_invented_organization(contract):
    contract.execute("INSERT INTO cost_identities VALUES('global-order','order',3,'global',0)")
    contract.execute("INSERT INTO finance_currency_settings VALUES('global',0,NULL)")
    assert contract.execute("SELECT currency FROM finance_currency_settings WHERE owner_scope='global'").fetchone()==(None,)


def test_unbound_same_org_attempt_cannot_claim_obligation(contract):
    obligation(contract)
    contract.execute("INSERT INTO cost_identities VALUES('other-attempt','attempt',9,'org',1)")
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute("INSERT INTO cost_attempt_participation VALUES('other-attempt','attempt','obligation-a','org',1,'started',NULL)")


def test_participation_history_cannot_be_deleted(contract):
    obligation(contract)
    contract.execute("INSERT INTO cost_attempt_participation VALUES('attempt-a','attempt','obligation-a','org',1,'started',NULL)")
    with pytest.raises(sqlite3.IntegrityError):contract.execute('DELETE FROM cost_attempt_participation')


@pytest.mark.parametrize('state,reason',[('unresolved',None),('unresolved',''),('observed','error'),('started','error')])
def test_participation_reason_matches_state(contract,state,reason):
    obligation(contract)
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute('INSERT INTO cost_attempt_participation VALUES(?,?,?,?,?,?,?)',('attempt-a','attempt','obligation-a','org',1,state,reason))


def test_observed_participation_cannot_regress_to_started(contract):
    obligation(contract)
    contract.execute("INSERT INTO cost_attempt_participation VALUES('attempt-a','attempt','obligation-a','org',1,'observed',NULL)")
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute("UPDATE cost_attempt_participation SET state='started'")


def test_bound_attempt_for_another_same_org_order_cannot_claim_obligation(contract):
    obligation(contract)
    for uid,kind in [('other-order','order'),('other-item','item'),('other-job','job'),('other-attempt','attempt')]:
        contract.execute('INSERT INTO cost_identities VALUES(?,?,?,?,?)',(uid,kind,9,'org',1))
    for uid,kind in [('other-item','item'),('other-job','job')]:
        contract.execute('INSERT INTO cost_order_memberships VALUES(?,?,?,?,?,?)',(uid,kind,'other-order','order','org',1))
    contract.execute("INSERT INTO cost_job_bindings VALUES('other-job','job','other-order','other-item','item','org',1)")
    contract.execute("INSERT INTO cost_attempt_bindings VALUES('other-attempt','attempt','other-job','other-order','other-item','org',1)")
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute("INSERT INTO cost_attempt_participation VALUES('other-attempt','attempt','obligation-a','org',1,'started',NULL)")


@pytest.mark.parametrize('statement',[
    "UPDATE cost_attempt_participation SET obligation_uid='obligation-b'",
    "DELETE FROM cost_attempt_bindings",
    "UPDATE cost_attempt_bindings SET job_uid='another-job'",
    "DELETE FROM cost_job_bindings",
    "UPDATE cost_job_bindings SET item_uid='another-item'",
])
def test_participation_and_binding_anchors_are_retained(contract,statement):
    obligation(contract)
    contract.execute("INSERT INTO cost_obligations SELECT 'obligation-b',order_uid,order_kind,item_uid,item_kind,owner_scope,owner_key,source_uid,source_kind,'printed-model-b',obligation_kind,required_quantity,evidence_state,evidence_reason FROM cost_obligations WHERE obligation_uid='obligation-a'")
    contract.execute("INSERT INTO cost_attempt_participation VALUES('attempt-a','attempt','obligation-a','org',1,'started',NULL)")
    with pytest.raises(sqlite3.IntegrityError):contract.execute(statement)
    assert contract.execute('SELECT obligation_uid FROM cost_attempt_participation').fetchone()==('obligation-a',)


def test_participation_can_progress_from_unresolved_to_observed_without_rewriting_anchors(contract):
    obligation(contract)
    contract.execute("INSERT INTO cost_attempt_participation VALUES('attempt-a','attempt','obligation-a','org',1,'started',NULL)")
    contract.execute("UPDATE cost_attempt_participation SET state='unresolved',reason='missing measurement'")
    contract.execute("UPDATE cost_attempt_participation SET state='observed',reason=NULL")
    assert contract.execute('SELECT attempt_uid,obligation_uid,state,reason FROM cost_attempt_participation').fetchone()==('attempt-a','obligation-a','observed',None)


@pytest.mark.parametrize('currency',['usd','US','USDD','XXX','',123,True,'USD; DROP TABLE cost_identities'])
def test_unsupported_or_noncanonical_currency_cannot_become_authority(contract,currency):
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute("INSERT INTO finance_currency_settings VALUES('org',1,?)",(currency,))


@pytest.mark.parametrize('currency',[None,'USD','EUR','GBP','CAD','AUD','JPY','CHF','NZD'])
def test_explicit_supported_currency_or_unknown_is_preserved(contract,currency):
    contract.execute("INSERT INTO finance_currency_settings VALUES('org',1,?)",(currency,))
    assert contract.execute("SELECT currency FROM finance_currency_settings WHERE owner_scope='org' AND owner_key=1").fetchone()==(currency,)
