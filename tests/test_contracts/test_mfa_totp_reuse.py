"""Authenticator steps are single-use across all of an account's code checks."""
import importlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from core.schema import bootstrap_database
from core.schema.migrator import MigrationError, apply_python_migration
from modules.organizations import routes_auth
from modules.organizations.mfa_attempts import consume_totp
from tests.test_contracts.test_education_classes_migration import native_engine, pg_engine
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_mfa_authentication_behavior import challenge
from tests.test_contracts.test_mfa_code_lockout import (
    CODES, DISABLE, VERIFY, account_app, assert_locked, assert_refused, both_databases,
    failure_times, submit,
)
from tests.test_contracts.test_mfa_recovery_codes import client, headers, issue

MIGRATION = importlib.import_module('core.schema.migrations.065_mfa_last_totp_step')


def stored_step(db):
    return db.execute(text('SELECT mfa_last_totp_step FROM users WHERE id=1')).scalar_one()


@pytest.mark.parametrize('offset', [-1, 0], ids=['previous', 'current'])
def test_accepts_unused_step(review_db, monkeypatch, offset):
    monkeypatch.setattr(pyotp.TOTP, 'timecode', lambda self, when: 1000)
    totp = pyotp.TOTP('JBSWY3DPEHPK3PXP')
    step = 1000 + offset
    assert stored_step(review_db) is None
    assert consume_totp(review_db, 1, totp.secret, totp.generate_otp(step))
    assert stored_step(review_db) == step


def test_wrong_and_reused_codes_execute_same_claim(review_db, monkeypatch):
    monkeypatch.setattr(pyotp.TOTP, 'timecode', lambda self, when: 1000)
    totp = pyotp.TOTP('JBSWY3DPEHPK3PXP')
    execute = Mock(wraps=review_db.execute)
    monkeypatch.setattr(review_db, 'execute', execute)

    def rejected_statements(code):
        execute.reset_mock()
        assert not consume_totp(review_db, 1, totp.secret, code)
        return [str(call.args[0]) for call in execute.call_args_list]

    wrong = rejected_statements('000000')
    assert stored_step(review_db) is None
    code = totp.generate_otp(1000)
    assert consume_totp(review_db, 1, totp.secret, code)
    reused = rejected_statements(code)
    assert wrong == reused
    assert len(reused) == 1 and reused[0].startswith('UPDATE users ')
    assert stored_step(review_db) == 1000


def test_reuse_counts_across_routes_and_older_unused_step_is_refused(client, review_db, monkeypatch):
    step = 1000
    monkeypatch.setattr(pyotp.TOTP, 'timecode', lambda self, when: step)
    totp, _ = issue(client)
    assert stored_step(review_db) == step
    step += 2
    assert submit(client, CODES, totp.now()).status_code == 200
    assert stored_step(review_db) == step
    assert_refused(submit(client, VERIFY, totp.generate_otp(step - 1)))
    for route in (VERIFY, DISABLE, CODES):
        assert_refused(submit(client, route, totp.now()))
    assert_locked(submit(client, VERIFY, totp.now()))
    assert len(failure_times(review_db)) == 5
    assert stored_step(review_db) == step


def test_matching_digits_in_adjacent_steps_cannot_be_reused(client, review_db, monkeypatch):
    step = 1000
    monkeypatch.setattr(pyotp.TOTP, 'timecode', lambda self, when: step)
    # Deterministically model two adjacent time steps displaying the same digits.
    monkeypatch.setattr(pyotp.TOTP, 'generate_otp', lambda self, counter: f'{1001 if counter == 1002 else counter:06}')
    totp, _ = issue(client)
    step += 1
    code = totp.now()
    assert submit(client, VERIFY, code).status_code == 200
    assert stored_step(review_db) == 1002
    step += 2
    assert_refused(submit(client, VERIFY, code))
    assert stored_step(review_db) == 1002


@pytest.mark.parametrize('admin', [False, True])
def test_reenroll_with_new_secret_in_same_step(client, review_db, monkeypatch, admin):
    monkeypatch.setattr(pyotp.TOTP, 'timecode', lambda self, when: 1000)
    old, codes = issue(client)
    if admin:
        response = client.delete('/admin/users/1/mfa', headers=headers('admin', 'admin'))
    else:
        response = submit(client, DISABLE, codes[0], recovery=True)
    assert response.status_code == 200
    assert stored_step(review_db) is None
    new, _ = issue(client, confirm_key="reenroll")
    different_secret = old.secret != new.secret
    assert different_secret
    assert stored_step(review_db) == 1000
    assert_refused(submit(client, VERIFY, new.now()))


@both_databases
def test_two_challenges_racing_one_code(account_app, native_engine, monkeypatch):
    step = 1000
    monkeypatch.setattr(pyotp.TOTP, 'timecode', lambda self, when: step)
    with TestClient(account_app) as caller:
        totp, _ = issue(caller)
    step += 1
    code = totp.now()
    ready = Barrier(2)
    lock_user = routes_auth.lock_for_code_check

    def synchronized(db, user_id):
        ready.wait(timeout=10)
        return lock_user(db, user_id)

    monkeypatch.setattr(routes_auth, 'lock_for_code_check', synchronized)

    def redeem(token):
        with TestClient(account_app) as caller:
            return submit(caller, VERIFY, code, token=token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = sorted(pool.map(redeem, [challenge(), challenge()]), key=lambda r: r.status_code)
    assert responses[0].status_code == 200
    assert_refused(responses[1])
    with native_engine.connect() as db:
        assert stored_step(db) == step
        assert len(failure_times(db)) == 1
        assert db.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one() == 1
        assert db.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one() == 1


@both_databases
def test_upgrade_existing_users_and_repeat_bootstrap(native_engine):
    bootstrap_database(native_engine)
    with native_engine.begin() as db:
        db.exec_driver_sql('ALTER TABLE users DROP COLUMN mfa_last_totp_step')
        db.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:mid'),
                   {'mid': MIGRATION.MIGRATION_ID})
        db.exec_driver_sql("INSERT INTO users(id,username,role,password_hash) VALUES(1,'student','viewer','fixture')")
    assert bootstrap_database(native_engine)['applied'] == [MIGRATION.MIGRATION_ID]
    with native_engine.begin() as db:
        assert stored_step(db) is None
        db.exec_driver_sql('UPDATE users SET mfa_last_totp_step=1000 WHERE id=1')
        MIGRATION.apply(db)
    assert bootstrap_database(native_engine)['applied'] == []
    with native_engine.connect() as db:
        MIGRATION.validate(db)
        assert stored_step(db) == 1000


@pytest.mark.parametrize('definition', ['TEXT', 'INTEGER DEFAULT 0', 'INTEGER NOT NULL'])
def test_migration_refuses_wrong_column_shape(definition):
    from sqlalchemy import create_engine
    engine = create_engine('sqlite://')
    try:
        with engine.begin() as db:
            db.exec_driver_sql(f'CREATE TABLE users (id INTEGER PRIMARY KEY, mfa_last_totp_step {definition})')
            with pytest.raises(MigrationError, match='column shape differs'):
                apply_python_migration(db, MIGRATION.__name__)
            assert db.execute(text('SELECT 1 FROM odin_schema_migrations WHERE migration_id=:mid'),
                              {'mid': MIGRATION.MIGRATION_ID}).first() is None
    finally:
        engine.dispose()
