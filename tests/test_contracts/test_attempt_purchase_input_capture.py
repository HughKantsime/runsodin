"""Historical purchase inputs captured through the actual first-start transaction."""
import pytest
from sqlalchemy import text
# Consumption now needs actual tray evidence (spool-tray-identity spec item 3), so purchase
# capture is exercised on the mapped plan; slot 1 / spool 7299 stays allocations[0].
from tests.test_contracts.test_attempt_spool_capture_behavior import capture_case, mapped_capture_case, capture, snapshot
from tests.test_contracts.test_education_review_workflow import review_db


def test_purchase_inputs_survive_catalog_edits_and_resume(mapped_capture_case):
    with mapped_capture_case.begin() as conn:
        conn.execute(text('UPDATE spools SET price=20,initial_weight_g=1000,spool_weight_g=250 WHERE id=7299'))
        assert capture(conn)
    original = snapshot(mapped_capture_case)
    inputs = original['material_plan']['allocations'][0]['purchase_inputs']
    assert inputs == {'state': 'recorded', 'reason': None, 'price': '20',
                      'net_weight_g': '1000', 'weight_basis': 'stored_net_filament_weight',
                      'currency': None, 'valuation_state': 'unknown_currency'}
    with mapped_capture_case.begin() as conn:
        conn.execute(text('UPDATE spools SET price=100,initial_weight_g=500,spool_weight_g=900 WHERE id=7299'))
        assert capture(conn) is False
    assert snapshot(mapped_capture_case) == original


@pytest.mark.parametrize('price,weight,reason', [
    (0, 1000, None), (None, 1000, 'missing_price'),
    (-1, 1000, 'invalid_price'), (float('inf'), 1000, 'invalid_price'),
    (20, None, 'missing_net_weight'), (20, 0, 'invalid_net_weight'),
    (20, -1, 'invalid_net_weight'), (20, float('inf'), 'invalid_net_weight'),
])
def test_purchase_unknowns_do_not_discard_consumption_identity(mapped_capture_case, price, weight, reason):
    with mapped_capture_case.begin() as conn:
        conn.execute(text('UPDATE spools SET price=:price,initial_weight_g=:weight WHERE id=7299'),
                     {'price': price, 'weight': weight})
        assert capture(conn)
    plan = snapshot(mapped_capture_case)['material_plan']
    assert plan['state'] == 'complete'
    inputs = plan['allocations'][0]['purchase_inputs']
    assert inputs['reason'] == reason
    assert inputs['state'] == ('recorded' if reason is None else 'unresolved')
    assert inputs['currency'] is None
    assert inputs['valuation_state'] == 'unknown_currency'
    if price == 0:
        assert inputs['price'] == '0'


def test_purchase_capture_rolls_back_with_caller(mapped_capture_case):
    with pytest.raises(RuntimeError, match='rollback'):
        with mapped_capture_case.begin() as conn:
            conn.execute(text('UPDATE spools SET price=20,initial_weight_g=1000 WHERE id=7299'))
            assert capture(conn)
            raise RuntimeError('rollback')
    with mapped_capture_case.connect() as conn:
        assert conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=7299')).scalar_one() is None


def test_fractional_purchase_inputs_preserve_canonical_decimal_strings(mapped_capture_case):
    with mapped_capture_case.begin() as conn:
        conn.execute(text('UPDATE spools SET price=12.99,initial_weight_g=750.25 WHERE id=7299'))
        assert capture(conn)
    inputs = snapshot(mapped_capture_case)['material_plan']['allocations'][0]['purchase_inputs']
    assert inputs['price'] == '12.99'
    assert inputs['net_weight_g'] == '750.25'
    assert inputs['currency'] is None


def test_owner_change_between_identity_and_purchase_reads_fails_closed(mapped_capture_case, monkeypatch):
    from modules.archives import attempt_accounting
    real_execute = attempt_accounting.execute
    changed = False
    def execute(conn, statement, parameters=()):
        nonlocal changed
        if statement.startswith('SELECT id,org_id') and 'WHERE id=?' in statement and parameters == (7299,) and not changed:
            changed = True
            real_execute(conn, 'UPDATE spools SET org_id=NULL,price=999 WHERE id=?', (7299,))
        return real_execute(conn, statement, parameters)
    monkeypatch.setattr(attempt_accounting, 'execute', execute)
    with mapped_capture_case.begin() as conn:
        assert capture(conn)
    plan = snapshot(mapped_capture_case)['material_plan']
    assert changed
    assert plan['state'] == 'unresolved'
    assert plan['reason'] == 'spool_identity_changed'
    assert plan['allocations'] == []
