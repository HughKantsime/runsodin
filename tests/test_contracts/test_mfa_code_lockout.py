"""MFA guessing limits through the enrolled user's three real code-check routes."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from core import auth
from core.schema import bootstrap_database
from modules.organizations import mfa_attempts
from tests.test_contracts.test_education_classes_migration import pg_engine
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_mfa_authentication_behavior import challenge
from tests.test_contracts.test_mfa_recovery_codes import CODES, client, headers, issue, make_app
from tests.test_contracts.test_password_reset_behavior import new_password

VERIFY = '/auth/mfa/verify'
DISABLE = '/auth/mfa'


def submit(caller, route, value, *, recovery=False, token=None):
    field = 'recovery_code' if recovery and route != CODES else 'code'
    if route == VERIFY:
        return caller.post(route, json={'mfa_token': token or challenge(), field: value})
    return caller.request('DELETE' if route == DISABLE else 'POST', route,
                          headers=headers(), json={field: value})


def wrong_totp(totp):
    candidates = (f'{(int(totp.now()) + i) % 1_000_000:06}' for i in range(1, 7))
    return next(code for code in candidates if not totp.verify(code, valid_window=2))


def assert_refused(response):
    assert response.status_code == 401
    assert response.json()['detail'] == 'Invalid MFA code'
    assert 'retry-after' not in response.headers


def assert_locked(response, seconds=900):
    assert response.status_code == 429
    assert response.headers['retry-after'] == str(seconds)
    minutes = (seconds + 59) // 60
    assert response.json()['detail'] == f"Too many wrong codes. Try again in {minutes} {'minute' if minutes == 1 else 'minutes'}."


@pytest.mark.parametrize('route', [VERIFY, DISABLE, CODES])
def test_fifth_wrong_totp_locks_account_before_evaluating_correct_code(client, monkeypatch, route):
    monkeypatch.setattr(mfa_attempts, 'time', lambda: 1000)
    totp, codes = issue(client)
    wrong = wrong_totp(totp)
    token = challenge()
    for _ in range(4):
        assert_refused(submit(client, route, wrong, token=token))
    assert_locked(submit(client, route, wrong, token=token))

    def unexpected(*args, **kwargs):
        pytest.fail('A locked account evaluated an MFA code')

    monkeypatch.setattr(mfa_attempts, 'valid_totp', unexpected)
    monkeypatch.setattr(mfa_attempts, 'consume_code', unexpected)
    for path in (VERIFY, CODES, DISABLE):
        assert_locked(submit(client, path, totp.now(), token=token))
        assert_locked(submit(client, path, codes[0], recovery=True, token=token))
    assert client.get(CODES, headers=headers()).json()['remaining'] == 10


def test_rolling_window_and_expiry_across_routes_and_addresses(client, review_db, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    totp, codes = issue(client)
    password = new_password()
    review_db.execute(text('UPDATE users SET password_hash=:p WHERE id=1'), {'p': auth.hash_password(password)})
    review_db.commit()
    t = 1000
    monkeypatch.setattr(mfa_attempts, 'time', lambda: t)
    wrong = wrong_totp(totp)
    wrong_recovery = 'ZZZZZ-ABCDE'
    assert all(c != wrong_recovery for c in codes)
    assert_refused(submit(client, VERIFY, wrong))
    t += 899
    for path in (DISABLE, CODES, VERIFY):
        assert_refused(submit(client, path, wrong_recovery, recovery=True))
    t += 1  # First failure has aged out. The three at t=1899 still count.
    assert_refused(submit(client, DISABLE, wrong))
    t += 890
    with TestClient(client.app, client=('192.0.2.42', 12345)) as other:
        assert_locked(submit(other, CODES, wrong_recovery, recovery=True))
    login = client.post('/auth/login', data={'username': 'student', 'password': password})
    assert login.status_code == 200 and login.json()['mfa_required']
    assert client.get('/auth/me', headers=headers()).status_code == 200
    accepted_totp = totp.now()
    assert_locked(submit(client, VERIFY, accepted_totp, token=login.json()['access_token']))
    t += 899
    assert_locked(submit(client, VERIFY, codes[0], recovery=True), 1)
    t += 1
    assert submit(client, VERIFY, codes[0], recovery=True).status_code == 200
    assert_refused(submit(client, CODES, wrong))
    audit = review_db.execute(text('SELECT * FROM audit_logs')).all()
    lock, = [row for row in audit if row.action == 'mfa_code_lockout']
    assert lock.entity_id == 1 and lock.ip_address == '192.0.2.42'
    assert json.loads(lock.details) == {'route': CODES}
    captured = caplog.text + str(audit)
    leaked = any(value in captured for value in [wrong, wrong_recovery, accepted_totp, *codes])
    assert not leaked


@pytest.mark.parametrize('use_recovery', [False, True])
def test_success_after_four_resets_other_routes(client, use_recovery):
    totp, codes = issue(client)
    wrong = wrong_totp(totp)
    for _ in range(4):
        assert_refused(submit(client, DISABLE, wrong))
    value = codes[0] if use_recovery else totp.now()
    assert submit(client, VERIFY, value, recovery=use_recovery).status_code == 200
    for _ in range(4):
        assert_refused(submit(client, CODES, wrong))
    assert_locked(submit(client, DISABLE, wrong))


def parallel_guesses(app, engine, monkeypatch):
    monkeypatch.setattr(mfa_attempts, 'time', lambda: 1000)
    with TestClient(app) as caller:
        totp, _ = issue(caller)
    wrong = wrong_totp(totp)
    barrier = Barrier(6)

    def guess(i):
        with TestClient(app, client=(f'192.0.2.{i + 1}', 12345)) as caller:
            barrier.wait(timeout=10)
            return submit(caller, (VERIFY, CODES, DISABLE)[i % 3], wrong).status_code

    with ThreadPoolExecutor(max_workers=6) as workers:
        assert sorted(workers.map(guess, range(6))) == [401] * 4 + [429] * 2
    with engine.connect() as db:
        times = db.execute(text('SELECT failure_times FROM mfa_code_attempts WHERE user_id=1')).scalar_one()
        assert len(json.loads(times)) == 5
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='mfa_code_lockout'")).scalar_one() == 1
        assert db.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one() == 0
    with TestClient(app) as caller:
        assert_locked(submit(caller, VERIFY, totp.now()))


def test_sqlite_parallel_guesses(client, review_db, monkeypatch):
    parallel_guesses(client.app, review_db.get_bind(), monkeypatch)


@pytest.mark.postgresql
def test_postgresql_parallel_guesses_and_account_lock(pg_engine, monkeypatch):
    check_upgrade_from_063(pg_engine)
    with pg_engine.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) "
                        "VALUES(1,'student','fixture','viewer',TRUE)"))
    parallel_guesses(make_app(pg_engine, monkeypatch), pg_engine, monkeypatch)


@pytest.mark.parametrize('path', ['/admin/users/4/mfa', '/users/4', '/users/4/erase'])
def test_removal_clears_only_target_account(client, review_db, path):
    totp, _ = issue(client, headers('outsider'))
    _, codes = issue(client)
    assert_refused(submit(client, CODES, 'wrong-recovery-code', recovery=True))
    token = challenge('outsider')
    wrong = wrong_totp(totp)
    for _ in range(4):
        assert_refused(submit(client, VERIFY, wrong, token=token))
    assert_locked(submit(client, VERIFY, wrong, token=token))
    assert client.delete(path, headers=headers('admin', 'admin')).status_code == 200
    assert review_db.execute(text('SELECT user_id FROM mfa_code_attempts')).scalars().all() == [1]
    assert submit(client, DISABLE, codes[0], recovery=True).status_code == 200
    assert review_db.execute(text('SELECT COUNT(*) FROM mfa_code_attempts')).scalar_one() == 0


@pytest.mark.parametrize('route', [DISABLE, '/auth/mfa/confirm', '/auth/mfa/enrollment/confirm'])
def test_address_limit_even_without_an_enrolled_factor(client, review_db, route):
    from modules.organizations import routes_mfa_enrollment
    client.app.include_router(routes_mfa_enrollment.router)
    for _ in range(10):
        assert submit(client, route, '123456').status_code in (400, 401)
    assert submit(client, route, '123456').status_code == 429
    assert review_db.execute(text('SELECT COUNT(*) FROM mfa_code_attempts')).scalar_one() == 0


@pytest.mark.parametrize('mandatory', [False, True])
def test_enrollment_ignores_failures_then_clears_them(client, review_db, mandatory):
    import pyotp
    from core.mfa_login import login_mfa_response
    from modules.organizations import routes_mfa_enrollment

    now = mfa_attempts.time()
    stored = json.dumps([now] * 5)
    review_db.execute(text("INSERT INTO mfa_code_attempts(user_id,failure_times) VALUES(1,:times)"),
                      {'times': stored})
    review_db.execute(text('UPDATE users SET mfa_enabled=NULL WHERE id=1'))
    if mandatory:
        review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"))
    review_db.commit()
    if mandatory:
        client.app.include_router(routes_mfa_enrollment.router)
        grant = login_mfa_response(review_db, 1)
        who = {'Authorization': 'Bearer ' + grant['access_token']}
        path = '/auth/mfa/enrollment'
    else:
        who = headers()
        path = '/auth/mfa'
    setup = client.post(path + '/setup', headers=who)
    assert setup.status_code == 200
    totp = pyotp.TOTP(setup.json()['secret'])
    for _ in range(5):
        assert client.post(path + '/confirm', headers=who, json={'code': wrong_totp(totp)}).status_code == 400
    assert review_db.execute(text('SELECT failure_times FROM mfa_code_attempts')).scalar_one() == stored
    assert client.post(path + '/confirm', headers=who, json={'code': totp.now()}).status_code == 200
    assert review_db.execute(text('SELECT COUNT(*) FROM mfa_code_attempts')).scalar_one() == 0


def check_upgrade_from_063(engine):
    migration = 'python:064-mfa-code-attempts'
    bootstrap_database(engine)
    with engine.begin() as db:
        db.execute(text('DROP TABLE mfa_code_attempts'))
        db.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id': migration})
    assert bootstrap_database(engine)['applied'] == [migration]
    assert bootstrap_database(engine)['applied'] == []
    with engine.begin() as db:
        db.execute(text("INSERT INTO users(id,username,role,password_hash) VALUES(20,'removed','viewer','fixture')"))
        db.execute(text("INSERT INTO mfa_code_attempts(user_id,failure_times) VALUES(20,'[100,101]')"))
        db.execute(text('DELETE FROM users WHERE id=20'))
        assert db.execute(text('SELECT COUNT(*) FROM mfa_code_attempts')).scalar_one() == 0


def test_sqlite_upgrade_from_063():
    engine = create_engine('sqlite://')
    try:
        check_upgrade_from_063(engine)
    finally:
        engine.dispose()


def test_address_ceiling_discloses_lock_only_for_a_current_challenge(client, monkeypatch):
    monkeypatch.setattr(mfa_attempts, 'time', lambda: 1000)
    totp, _ = issue(client)
    spent = challenge()
    assert submit(client, VERIFY, totp.now(), token=spent).status_code == 200
    for i in range(9):
        response = submit(client, VERIFY, wrong_totp(totp))
        assert response.status_code == (401 if i < 4 else 429)
    for token in (spent, 'invalid'):
        response = submit(client, VERIFY, totp.now(), token=token)
        assert response.status_code == 429
        assert 'Too many wrong codes' not in response.json()['detail']
    assert_locked(submit(client, VERIFY, totp.now()))
