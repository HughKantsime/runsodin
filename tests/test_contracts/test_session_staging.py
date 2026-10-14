"""Session authority can participate in a caller's atomic transaction."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('valid', [True, False])
def test_staging_never_owns_caller_transaction(review_db, monkeypatch, valid):
    from core.auth import create_access_token
    from core.session_tracking import stage_session

    db = review_db
    db.execute(text("UPDATE users SET email='owned-stage@example.test' WHERE id=1"))
    token = create_access_token({'sub': 'student' if valid else 'missing-owned-user', 'role': 'viewer'})
    original_commit, original_rollback = db.commit, db.rollback
    transaction_calls = []

    def forbidden():
        transaction_calls.append('called')
        raise AssertionError('staging must not commit or rollback caller transaction')

    monkeypatch.setattr(db, 'commit', forbidden)
    monkeypatch.setattr(db, 'rollback', forbidden)
    assert stage_session(db, 1, token, '192.0.2.1', 'owned-stage') is valid
    assert transaction_calls == []
    assert db.execute(text('SELECT email FROM users WHERE id=1')).scalar_one() == 'owned-stage@example.test'
    assert db.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=1')).scalar_one() == int(valid)
    monkeypatch.setattr(db, 'commit', original_commit)
    monkeypatch.setattr(db, 'rollback', original_rollback)
    db.rollback()
    with Session(db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT email FROM users WHERE id=1')).scalar_one() != 'owned-stage@example.test'
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=1')).scalar_one() == 0


def test_staged_session_and_unrelated_change_commit_together(review_db):
    from core.auth import create_access_token
    from core.session_tracking import stage_session

    review_db.execute(text("UPDATE users SET email='owned-stage@example.test' WHERE id=1"))
    assert stage_session(review_db, 1, create_access_token({'sub':'student','role':'viewer'}), '192.0.2.1', 'x' * 600)
    review_db.commit()
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT email FROM users WHERE id=1')).scalar_one() == 'owned-stage@example.test'
        assert fresh.execute(text('SELECT length(user_agent) FROM active_sessions WHERE user_id=1')).scalar_one() == 500
