"""Security rulings with real tokens and transactions, and the existing fake reader."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from core.models import AuditLog, SystemConfig
from modules.orders import square_store as store
from modules.orders.square_client import SquareReader
from ops.release_control.run_gate import SECRET_PATTERNS
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_square_readonly_workflow import shop, mapped, connect
from tests.test_contracts.test_square_setup_delegation import authenticate, payload, path, setup_policy
from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers


def saved(shop):
    with Session(shop[3].get_bind()) as db:
        return (db.execute(select(SystemConfig.value).where(SystemConfig.key == store.key(1))).scalar_one(),
                db.execute(select(AuditLog.id, AuditLog.details).where(
                    AuditLog.action != 'square.setup_refused').order_by(AuditLog.id)).all())


def test_square_ruling_ids_are_safe_for_gate_output(request):
    for item in request.node.parent.collect():
        for label, pattern in SECRET_PATTERNS:
            assert not pattern.search(item.nodeid), f"test id matches {label}"


@pytest.mark.parametrize('change', [{'variation_id': 'V2'}, {'location_id': 'L2'}])
def test_token_cannot_remap_a_mapped_product(shop, change):
    status = mapped(shop)
    authenticate(shop)
    before = saved(shop)
    shop[2].clear()
    result = shop[0].put(path(), json=payload(status['revision'], **change))
    assert result.status_code == 409
    assert result.json()['detail'] == 'This product is already mapped in Square. A signed-in administrator must change the mapping.'
    assert saved(shop) == before and shop[2] == []


def test_token_same_mapping_is_a_no_change(shop):
    status = mapped(shop)
    result = shop[0].put(path(True), json=payload(status['revision'], True))
    status = result.json()['connection']
    authenticate(shop, ['read', 'write:square_setup'])
    before = saved(shop)
    shop[2].clear()
    result = shop[0].put(path(), json=payload(status['revision']))
    assert result.status_code == 200 and result.json() == status
    assert saved(shop) == before and shop[2] == []


def test_token_cannot_rebaseline(shop):
    status = mapped(shop)
    authenticate(shop)
    result = shop[0].put(path(True), json=payload(status['revision'], True))
    assert result.status_code == 200
    before = saved(shop)
    shop[2].clear()
    result = shop[0].put(path(True), json=payload(result.json()['connection']['revision'], True))
    assert result.status_code == 409
    assert result.json()['detail'] == 'This product already has a stock baseline. A signed-in administrator must change it.'
    assert saved(shop) == before and shop[2] == []


@pytest.mark.parametrize('global_owner', [False, True])
def test_two_token_baselines_race_one_wins(shop, monkeypatch, global_owner):
    status = mapped(shop)
    if global_owner:
        # Exercise the connector fence too: owner 0 has no group row to lock.
        db = shop[3]
        doc, _ = store.load(db, 1)
        doc['tenant'] = 0
        db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
        db.execute(text('UPDATE products SET org_id=NULL WHERE id=101'))
        status = store.save(db, 0, doc, None)
        db.commit()
    authenticate(shop)
    real_count, barrier = SquareReader.count, Barrier(2)
    def count(self, *args):
        result = real_count(self, *args)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(SquareReader, 'count', count)
    def call():
        return shop[0].put(path(True), json=payload(status['revision'], True)).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(call), pool.submit(call)
        assert sorted([first.result(timeout=20), second.result(timeout=20)]) == [200, 409]
    with Session(shop[3].get_bind()) as db:
        rows = db.execute(select(AuditLog).where(AuditLog.action == 'square.stock_baseline')).scalars().all()
        assert len(rows) == 1 and rows[0].details['quantity'] == 7
        assert store.load(db, 0 if global_owner else 1)[0]['mappings']['101']['stock_baseline']['quantity'] == 7


def test_token_baseline_reads_square_and_refuses_a_different_count(shop):
    status = mapped(shop)
    authenticate(shop)
    before = saved(shop)
    shop[2].clear()
    result = shop[0].put(path(True), json=payload(status['revision'], True, quantity=8))
    assert result.status_code == 409
    assert result.json()['detail'] == 'Square shows 7.00000; you stated 8. Nothing was changed.'
    assert shop[2][-1][1] == '/v2/inventory/counts/batch-retrieve'
    assert shop[2][-1][3]['catalog_object_ids'] == ['V1']
    assert shop[2][-1][3]['location_ids'] == ['L1']
    assert saved(shop) == before


def test_token_baseline_reads_square_and_refuses_when_square_is_unreachable(shop, monkeypatch):
    status = mapped(shop)
    authenticate(shop)
    before = saved(shop)
    def unreachable(*args):
        raise HTTPException(502, 'Square read failed. No stock was changed. Try again.')
    monkeypatch.setattr(SquareReader, 'count', unreachable)
    result = shop[0].put(path(True), json=payload(status['revision'], True))
    assert result.status_code == 502 and saved(shop) == before


def test_token_baseline_reads_square_and_refused_under_itar_without_a_call(shop, monkeypatch):
    from modules.orders.square_client import ITAR_REFUSAL
    status = mapped(shop)
    authenticate(shop)
    before = saved(shop)
    shop[2].clear()
    monkeypatch.setenv('ODIN_ITAR_MODE', '1')
    result = shop[0].put(path(True), json=payload(status['revision'], True))
    assert result.status_code == 409 and result.json()['detail'] == ITAR_REFUSAL
    assert shop[2] == [] and saved(shop) == before


@pytest.mark.parametrize('problem', ['unknown', 'untracked'])
def test_token_baseline_refuses_unknown_or_untracked_count(shop, monkeypatch, problem):
    status = mapped(shop)
    authenticate(shop)
    before = saved(shop)
    if problem == 'unknown':
        monkeypatch.setattr(SquareReader, 'count', lambda *a: {'state': 'unknown', 'quantity': None})
    else:
        monkeypatch.setattr(SquareReader, 'variation', lambda *a: {'tracked': False})
    assert shop[0].put(path(True), json=payload(status['revision'], True)).status_code == 409
    assert saved(shop) == before


@pytest.mark.parametrize('seconds,expected', [(-901, 422), (-900, 200), (60, 200), (61, 422)])
def test_token_baseline_time_window(shop, monkeypatch, seconds, expected):
    from modules.orders import stock_baseline
    now = datetime.now(timezone.utc)
    class ClockType(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)
    class Clock(datetime, metaclass=ClockType):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(stock_baseline, 'datetime', Clock)
    status = mapped(shop)
    authenticate(shop)
    before = saved(shop)
    result = shop[0].put(path(True), json=payload(status['revision'], True, at=(now + timedelta(seconds=seconds)).isoformat()))
    assert result.status_code == expected, result.text
    if expected == 422:
        assert '15 minutes' in result.json()['detail'] and '60 seconds' in result.json()['detail']
        assert saved(shop) == before


@pytest.mark.parametrize('delegated', [False, True])
def test_baseline_overflow_time_is_422(shop, delegated):
    status = mapped(shop)
    if delegated:
        authenticate(shop)
    before = saved(shop)
    result = shop[0].put(path(True), json=payload(status['revision'], True, at='9999-12-31T23:59:59-10:00'))
    assert result.status_code == 422 and saved(shop) == before


def test_delegated_audit_rows_say_what_was_mapped(shop):
    status, _ = connect(shop[0])
    authenticate(shop)
    assert shop[0].put(path(), json=payload(status['revision'])).status_code == 200
    row = saved(shop)[1][-1].details
    assert row.items() >= {'location_id': 'L1', 'variation_id': 'V1', 'item_name': 'Part',
                           'variation_name': 'Regular', 'previous': None}.items()


@pytest.mark.parametrize('enabled', [False, True])
def test_noop_grant_writes_no_audit_row(shop, enabled):
    mapped(shop)
    token = authenticate(shop, ['read'])
    url = f'/tokens/{token}/square-setup'
    assert shop[0].put(url, headers=interactive_headers(), json={'enabled': enabled}).status_code == 200
    before = saved(shop)
    assert shop[0].put(url, headers=interactive_headers(), json={'enabled': enabled}).status_code == 200
    assert saved(shop) == before


@pytest.mark.parametrize('baseline', [False, True])
@pytest.mark.parametrize('note,expected', [
    ('  Approved by owner\t in work order 12\n', 200), ('✓ approved', 200),
    ('\u200b\u200d\x01\t ', 422), ('\u0301', 422), ('approved\x00', 422),
    ('password=synthetic', 422), ('https://example.test/?code=synthetic', 422),
    ('odin_' + 'a' * 43, 422), ('Authorization: Bearer synthetic', 422),
], ids=['padded-note', 'check-mark', 'invisible-only', 'combining-only', 'nul',
        'password-pair', 'code-url', 'odin-token-shape', 'bearer-header'])
def test_approval_note_is_stored_verbatim_or_refused(shop, baseline, note, expected):
    status = mapped(shop) if baseline else connect(shop[0])[0]
    authenticate(shop)
    before = saved(shop)
    shop[2].clear()
    result = shop[0].put(path(baseline), json=payload(status['revision'], baseline, approval_note=note))
    assert result.status_code == expected, result.text
    if expected == 200:
        assert saved(shop)[1][-1].details['approval_note'] == note
    else:
        assert saved(shop) == before and shop[2] == []
        if expected == 422 and ('=' in note or note.startswith(('odin_', 'Authorization:'))):
            assert result.json()['detail'] == 'Remove anything that looks like a password, token or code from the note.'


@pytest.mark.parametrize('baseline', [False, True])
@pytest.mark.parametrize('change', ['revoke', 'delete', 'expire', 'inactive', 'role', 'owner', 'education', 'permission'])
def test_revoke_during_request_stops_the_write(shop, monkeypatch, baseline, change):
    status = mapped(shop) if baseline else connect(shop[0])[0]
    token = authenticate(shop)
    before = saved(shop)
    original = SquareReader.count if baseline else SquareReader.variation
    def changed(self, *args):
        result = original(self, *args)
        with Session(shop[3].get_bind()) as db:
            statements = {
                'revoke': "UPDATE api_tokens SET scopes='[\"read\"]' WHERE id=:id",
                'delete': 'DELETE FROM api_tokens WHERE id=:id',
                'expire': "UPDATE api_tokens SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=:id",
                'inactive': 'UPDATE users SET is_active=FALSE WHERE id=3',
                'role': "UPDATE users SET role='operator' WHERE id=3",
                'owner': 'UPDATE api_tokens SET user_id=5 WHERE id=:id',
            }
            if change == 'education':
                db.add(SystemConfig(key='education_mode', value=True))
            elif change == 'permission':
                db.add(SystemConfig(key='rbac_permissions', value={'action_access': {'products.square_manage': []}}))
            else:
                db.execute(text(statements[change]), {'id': token})
            db.commit()
        return result
    monkeypatch.setattr(SquareReader, 'count' if baseline else 'variation', changed)
    result = shop[0].put(path(baseline), json=payload(status['revision'], baseline))
    assert result.status_code == 403, result.text
    assert saved(shop) == before


def education_mode_gate(shop):
    status = mapped(shop)
    token = authenticate(shop)
    shop[3].add(SystemConfig(key='education_mode', value=True))
    shop[3].commit()
    before = saved(shop)
    shop[2].clear()
    assert shop[0].put(f'/tokens/{token}/square-setup', headers=interactive_headers(), json={'enabled': True}).status_code == 403
    for baseline in (False, True):
        assert shop[0].put(path(baseline), json=payload(status['revision'], baseline)).status_code == 403
    assert saved(shop) == before and shop[2] == []


def test_education_mode_boolean_gate(shop):
    education_mode_gate(shop)


def test_only_the_validated_delegated_note_bypasses_audit_redaction(shop):
    from core.audit import log_audit
    from core.log_redaction import REDACTED
    db = shop[3]
    note = '  Approved by owner in work order 12\n'
    row = log_audit(db, 'square.product_mapped', 'product', 101,
                    {'auth_kind': 'user_api_token', 'approval_note': note, 'item_name': 'password=synthetic'})
    assert row.details == {'auth_kind': 'user_api_token', 'approval_note': note,
                           'item_name': 'password=' + REDACTED}
    with pytest.raises(HTTPException) as error:
        log_audit(db, 'square.stock_baseline', 'product', 101,
                  {'auth_kind': 'user_api_token', 'approval_note': 'password=synthetic'})
    assert error.value.status_code == 422
    other = log_audit(db, 'other.action', details={'approval_note': 'password=synthetic'})
    assert other.details['approval_note'] == 'password=' + REDACTED
    db.rollback()


def test_token_baseline_connection_change_during_read_stops_the_write(shop, monkeypatch):
    status = mapped(shop)
    authenticate(shop)
    original, after = SquareReader.count, []
    def changed(self, *args):
        result = original(self, *args)
        with Session(shop[3].get_bind()) as db:
            doc, encoded = store.load(db, 1)
            store.save(db, 1, doc, encoded)
            db.commit()
        after.append(saved(shop))
        return result
    monkeypatch.setattr(SquareReader, 'count', changed)
    assert shop[0].put(path(True), json=payload(status['revision'], True)).status_code == 409
    assert saved(shop) == after[0]


@pytest.mark.parametrize('baseline', [False, True])
@pytest.mark.parametrize('note,expected', [(' ', 422), ('', 422), ('  ' + 'x' * 500 + '  ', 200)])
def test_session_note_validation_keeps_its_trimmed_length_rule(shop, baseline, note, expected):
    status = mapped(shop)
    result = shop[0].put(path(baseline), json=payload(status['revision'], baseline, approval_note=note))
    assert result.status_code == expected, result.text
