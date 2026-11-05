"""Committed acceptance survives an audit failure; ambiguous storage fails closed."""
from contextlib import nullcontext

import pytest
from sqlalchemy import event, insert, select, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('failure', ['audit', 'before_commit', 'after_commit', 'fallback'])
def test_accepted_outcome_recovery(review_db, monkeypatch, caplog, failure):
    from core import audit, dispatch_claims
    engine = review_db.get_bind()
    table = dispatch_claims.TABLE
    token = 'a' * 32
    review_db.execute(insert(table).values(printer_id=9, job_id=1, physical_key='a'*64,
        configuration_key='b'*64, payload_sha256='c'*64, token=token, state='adapter_entered'))
    review_db.commit()
    original_commit = Session.commit
    if failure in ('audit', 'fallback'):
        def refuse(db, *args, **kwargs):
            # A real SQL error (also aborts the transaction on PostgreSQL).
            db.execute(text('SELECT * FROM owned_missing_audit_table'))
        monkeypatch.setattr(audit, 'log_audit', refuse)
    else:
        def fail_commit(db):
            if failure == 'after_commit':
                original_commit(db)
            raise RuntimeError('Owned commit reporting failure')
        monkeypatch.setattr(Session, 'commit', fail_commit)
    calls = 0
    def refuse_fallback(conn, cursor, statement, params, context, executemany):
        nonlocal calls
        if statement.startswith('UPDATE printer_dispatch_claims'):
            calls += 1
            if calls == 2:
                raise RuntimeError('Owned fallback storage failure')
    if failure == 'fallback':
        event.listen(engine, 'before_cursor_execute', refuse_fallback)
    try:
        assert dispatch_claims.record_outcome(engine, token, 'accepted', audit_actor=lambda db: nullcontext()) == (failure != 'fallback')
    finally:
        if failure == 'fallback':
            event.remove(engine, 'before_cursor_execute', refuse_fallback)
    with engine.connect() as conn:
        assert conn.execute(select(table.c.state)).scalar_one() == ('adapter_entered' if failure == 'fallback' else 'accepted')
        assert conn.execute(text("SELECT count(*) FROM audit_logs WHERE action='job.dispatched'")).scalar_one() == (1 if failure == 'after_commit' else 0)
    assert any(r.name == 'odin.dispatch' and r.levelname == 'ERROR' and
               'Accepted dispatch audit row could not be recorded' in r.message for r in caplog.records)
    if failure != 'fallback':
        # Exception handlers in orchestration cannot downgrade accepted state.
        assert not dispatch_claims.record_outcome(engine, token, 'uncertain')
