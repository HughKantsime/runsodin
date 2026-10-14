"""SQLite-specific replacement semantics, independent of recursive trigger setting."""
import sqlite3
import pytest
from tests.test_contracts.test_cost_identity_design_contract import contract, obligation

@pytest.mark.parametrize('recursive',[False,True])
@pytest.mark.parametrize('target',['identity','obligation'])
def test_replace_cannot_rewrite_retained_cost_evidence(contract,recursive,target):
    contract.execute('PRAGMA recursive_triggers='+('ON' if recursive else 'OFF'))
    if target=='identity':
        statement="INSERT OR REPLACE INTO cost_identities VALUES('spool-a','spool',999,'org',1)"
    else:
        obligation(contract)
        statement="INSERT OR REPLACE INTO cost_obligations SELECT obligation_uid,order_uid,order_kind,item_uid,item_kind,owner_scope,owner_key,source_uid,source_kind,allocation_key,obligation_kind,'999',evidence_state,evidence_reason FROM cost_obligations WHERE obligation_uid='obligation-a'"
    with pytest.raises(sqlite3.IntegrityError):contract.execute(statement)
    if target=='identity':assert contract.execute("SELECT resource_id FROM cost_identities WHERE identity_uid='spool-a'").fetchone()==(1,)
    else:assert contract.execute("SELECT required_quantity FROM cost_obligations").fetchone()==('2',)


def test_replacement_cannot_swap_obligation_identity_via_allocation_unique_key(contract):
    contract.execute('PRAGMA recursive_triggers=OFF')
    obligation(contract)
    with pytest.raises(sqlite3.IntegrityError):
        contract.execute("INSERT OR REPLACE INTO cost_obligations SELECT 'replacement',order_uid,order_kind,item_uid,item_kind,owner_scope,owner_key,source_uid,source_kind,allocation_key,obligation_kind,required_quantity,evidence_state,evidence_reason FROM cost_obligations WHERE obligation_uid='obligation-a'")
    assert contract.execute('SELECT obligation_uid FROM cost_obligations').fetchone()==('obligation-a',)


@pytest.mark.parametrize('target',['identity','obligation'])
def test_referenced_cost_evidence_cannot_be_replaced(contract,target):
    contract.execute('PRAGMA recursive_triggers=OFF')
    obligation(contract)
    contract.execute("INSERT INTO cost_attempt_participation VALUES('attempt-a','attempt','obligation-a','org',1,'started',NULL)")
    if target=='identity':
        statement="INSERT OR REPLACE INTO cost_identities VALUES('item-a','item',999,'org',1)"
    else:
        statement="INSERT OR REPLACE INTO cost_obligations SELECT obligation_uid,order_uid,order_kind,item_uid,item_kind,owner_scope,owner_key,source_uid,source_kind,allocation_key,obligation_kind,'999',evidence_state,evidence_reason FROM cost_obligations WHERE obligation_uid='obligation-a'"
    with pytest.raises(sqlite3.IntegrityError):contract.execute(statement)
    assert contract.execute('SELECT obligation_uid FROM cost_attempt_participation').fetchone()==('obligation-a',)
