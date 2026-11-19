"""MFA guessing limits through the enrolled user's three real code-check routes."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text

from core import auth
from core.models import AuditLog
from core.schema import bootstrap_database
from modules.organizations import mfa_attempts
from tests.test_contracts.test_education_classes_migration import native_engine, pg_engine
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_mfa_authentication_behavior import challenge
from tests.test_contracts.test_mfa_recovery_codes import CODES, client, headers, issue, make_app
from tests.test_contracts.test_password_reset_behavior import new_password

VERIFY = '/auth/mfa/verify'
DISABLE = '/auth/mfa'
POSTGRES = pytest.param('postgresql', marks=pytest.mark.postgresql)
both_databases = pytest.mark.parametrize('native_engine', ['sqlite', POSTGRES], indirect=True)


def seed_student(engine):
    with engine.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) "
                        "VALUES(1,'student','fixture','viewer',TRUE)"))


@pytest.fixture
def account_app(native_engine, monkeypatch):
    bootstrap_database(native_engine)
    seed_student(native_engine)
    return make_app(native_engine, monkeypatch)


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


def test_regeneration_requires_a_code_before_checking_it(client, review_db, monkeypatch):
    issue(client)
    from modules.organizations import routes_auth
    monkeypatch.setattr(routes_auth, 'require_code', lambda *a, **kw: pytest.fail('Checked a blank code'))
    for body in ({}, {'code': ''}, {'code': None}, {'code': ' \t'}, None):
        response = client.post(CODES, headers={**headers(), 'Content-Type': 'application/json'},
                               content=json.dumps(body))
        assert response.status_code == 400
        assert response.json()['detail'] == 'Enter an authenticator or recovery code.'
    assert review_db.execute(text('SELECT COUNT(*) FROM mfa_code_attempts')).scalar_one() == 0


def test_regeneration_counts_wrong_values_of_any_json_type(client):
    issue(client)
    for wrong in (False, 0, [], {}):
        assert_refused(submit(client, CODES, wrong))
    assert_locked(submit(client, CODES, 'wrong'))


def seed_failures(db, stored):
    db.execute(text('INSERT INTO mfa_code_attempts(user_id,failure_times) VALUES(1,:times)'), {'times': stored})
    db.commit()


def failure_times(db):
    return json.loads(db.execute(text('SELECT failure_times FROM mfa_code_attempts WHERE user_id=1')).scalar_one())


@pytest.mark.parametrize('stored', ['broken-json', '{"unexpected":1}', '[999,"invalid"]', '[true]', '[NaN]'])
def test_malformed_history_is_replaced_on_next_wrong_code(client, review_db, monkeypatch, caplog, stored):
    issue(client)
    monkeypatch.setattr(mfa_attempts, 'time', lambda: 1000)
    seed_failures(review_db, stored)
    caplog.clear()
    assert_refused(submit(client, VERIFY, 'wrong', recovery=True))
    assert failure_times(review_db) == [1000]
    warning, = [record for record in caplog.records if record.name == mfa_attempts.__name__]
    assert warning.levelno == logging.WARNING and stored not in warning.getMessage()


def test_clock_rollback_ignores_future_failures_and_bounds_retry(client, review_db, monkeypatch):
    totp, _ = issue(client)
    now = 1000
    monkeypatch.setattr(mfa_attempts, 'time', lambda: now)
    seed_failures(review_db, json.dumps([999] * 4 + [now + 10**12]))
    assert_locked(submit(client, VERIFY, 'wrong', recovery=True), mfa_attempts.LOCKOUT_SECONDS)
    assert failure_times(review_db) == [999] * 4 + [now]
    now += mfa_attempts.LOCKOUT_SECONDS
    assert submit(client, VERIFY, totp.now()).status_code == 200


@pytest.mark.parametrize('stored', ['broken-json', '[1001,1001,1001,1001,1001]'])
def test_correct_code_clears_unusable_history(client, review_db, monkeypatch, stored):
    totp, _ = issue(client)
    monkeypatch.setattr(mfa_attempts, 'time', lambda: 1000)
    seed_failures(review_db, stored)
    assert submit(client, VERIFY, totp.now()).status_code == 200
    assert review_db.execute(text('SELECT COUNT(*) FROM mfa_code_attempts')).scalar_one() == 0


def test_failed_lockout_audit_preserves_fifth_failure(client, review_db, monkeypatch, caplog):
    totp, _ = issue(client)
    monkeypatch.setattr(mfa_attempts, 'time', lambda: 1000)
    for _ in range(4):
        assert_refused(submit(client, VERIFY, 'wrong', recovery=True))

    def refuse_insert(mapper, connection, target):
        if target.action == 'mfa_code_lockout':
            raise RuntimeError('private audit failure detail')

    event.listen(AuditLog, 'before_insert', refuse_insert)
    try:
        assert_locked(submit(client, VERIFY, 'wrong', recovery=True))
    finally:
        event.remove(AuditLog, 'before_insert', refuse_insert)
    assert failure_times(review_db) == [1000] * 5
    assert_locked(submit(client, VERIFY, totp.now()))
    assert any('audit' in r.getMessage().lower() for r in caplog.records if r.name == mfa_attempts.__name__)
    assert 'private audit failure detail' not in caplog.text


@both_databases
@pytest.mark.parametrize('route', [VERIFY, DISABLE, CODES])
def test_fifth_wrong_totp_locks_account_before_evaluating_correct_code(account_app, monkeypatch, route):
    client = TestClient(account_app)
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


@both_databases
def test_rolling_window_and_expiry_across_routes_and_addresses(account_app, native_engine, monkeypatch, caplog):
    client = TestClient(account_app)
    caplog.set_level(logging.INFO)
    totp, codes = issue(client)
    password = new_password()
    with native_engine.begin() as db:
        db.execute(text('UPDATE users SET password_hash=:p WHERE id=1'), {'p': auth.hash_password(password)})
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
    with native_engine.connect() as db:
        audit = db.execute(text('SELECT * FROM audit_logs')).all()
    lock, = [row for row in audit if row.action == 'mfa_code_lockout']
    assert lock.entity_id == 1 and lock.ip_address == '192.0.2.42'
    details = json.loads(lock.details) if isinstance(lock.details, str) else lock.details
    assert details == {'route': CODES}
    captured = caplog.text + str(audit)
    leaked = any(value in captured for value in [wrong, wrong_recovery, accepted_totp, *codes])
    assert not leaked


@both_databases
@pytest.mark.parametrize('use_recovery', [False, True])
def test_success_after_four_resets_other_routes(account_app, use_recovery):
    client = TestClient(account_app)
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


@pytest.mark.parametrize('native_engine', [POSTGRES], indirect=True)
def test_postgresql_parallel_guesses_and_account_lock(native_engine, monkeypatch):
    check_upgrade_from_063(native_engine)
    seed_student(native_engine)
    parallel_guesses(make_app(native_engine, monkeypatch), native_engine, monkeypatch)


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
