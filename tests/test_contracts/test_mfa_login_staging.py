"""MFA enrollment staging preserves ownership of the caller's transaction."""
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('commit', [True, False])
def test_enrollment_insert_failure_respects_transaction_owner(review_db, monkeypatch, commit):
    from core.mfa_login import login_mfa_response

    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"))
    review_db.commit()
    review_db.execute(text("UPDATE users SET email='pending@example.test' WHERE id=1"))
    original_execute = review_db.execute

    def refuse_enrollment(statement, *args, **kwargs):
        if str(statement).startswith('INSERT INTO mfa_enrollments'):
            raise RuntimeError('synthetic enrollment insert failure')
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(review_db, 'execute', refuse_enrollment)
    with pytest.raises(HTTPException) as error:
        login_mfa_response(review_db, 1, commit=commit)
    assert error.value.status_code == 503
    assert review_db.execute(text('SELECT email FROM users WHERE id=1')).scalar_one() == (
        'student@example.test' if commit else 'pending@example.test')
    assert review_db.execute(text('SELECT COUNT(*) FROM mfa_enrollments')).scalar_one() == 0
    review_db.rollback()
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT email FROM users WHERE id=1')).scalar_one() == 'student@example.test'
