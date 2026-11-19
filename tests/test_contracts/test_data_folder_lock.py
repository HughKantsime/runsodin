"""Temporary SQLite folders, real locks and the ordinary command entry points."""
import fcntl
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from core import dispatch_target_lock as locks
from core.dispatch_identity import build_identity
from core.errors import OdinError
from tests.test_contracts.test_dispatch_target_lock import engine
from tests.test_contracts.test_dispatch_target_entrypoints import inputs, no_devices, setup, review_db
from tests.test_contracts.test_power_fence_runtime import runtime, token_client
from tests.test_contracts.test_education_followup_queues import order, queued
from tests.test_contracts.test_profile_library_behavior import profile_client
from tests.test_contracts.test_dispatch_recovery_actions import actions, control_client, recovery_client
from tests.test_contracts.test_dispatch_observation_binding_behavior import observed

KEY = 'a' * 64


@pytest.mark.parametrize('magic', [0x65735546, 0x6a656a63], ids=['FUSE', 'virtiofs'])
@pytest.mark.parametrize('behavior', ['working', 'no_exclusion', 'still_blocked', 'error'])
def test_shared_mount_requires_contention_and_release(engine, monkeypatch, magic, behavior):
    monkeypatch.setattr(locks, '_filesystem_type', lambda fd: magic)
    real = fcntl.flock
    calls = []
    def flock(fd, operation):
        calls.append((fd, operation, os.fstat(fd).st_ino))
        if behavior == 'no_exclusion':
            return
        if behavior == 'error':
            raise OSError
        if behavior == 'still_blocked' and len(calls) == 4:
            raise BlockingIOError
        return real(fd, operation)
    monkeypatch.setattr(fcntl, 'flock', flock)
    result = locks.acquire_target_lock(engine, KEY)
    directory = Path(engine.url.database + '.odin-sender-locks')
    assert list(directory.iterdir()) == [directory / (KEY + '.lock')]
    if behavior == 'working':
        assert result.status == 'acquired' and result.handle.held()
        assert calls[0][0] != calls[1][0] == calls[3][0]
        assert calls[0][2] == calls[1][2] != calls[4][2]
        assert calls[2][1] == fcntl.LOCK_UN
        assert result.handle.close().released
    else:
        assert (result.status, result.reason) == ('unsupported', 'filesystem')
        assert ('FUSE' if magic == 0x65735546 else 'virtiofs') in result.message


@pytest.mark.parametrize('magic,label', [(0x6969, 'NFS'), (0xff534d42, 'CIFS'), (0xfe534d42, 'SMB'),
                                         (0x01021997, '9p'), (0xdeadbeef, '0xdeadbeef')])
def test_network_and_unknown_mounts_never_probe(engine, monkeypatch, magic, label):
    monkeypatch.setattr(locks, '_filesystem_type', lambda fd: magic)
    monkeypatch.setattr(locks, '_prove_exclusion', lambda fd: pytest.fail('Unexpected probe'))
    result = locks.acquire_target_lock(engine, KEY)
    assert (result.status, result.reason) == ('unsupported', 'filesystem')
    assert label in result.message
    assert not Path(engine.url.database + '.odin-sender-locks').exists()


@contextmanager
def folder_mode(engine, mode):
    path = Path(engine.url.database).parent
    original = path.stat().st_mode & 0o7777
    mask = os.umask(0o002)
    try:
        path.chmod(mode)
        yield (f'The ODIN data folder is writable by other users (mode {mode:o}). '
               'Set it to 755 (chmod go-w on the folder), then try again. No command was sent.')
    finally:
        path.chmod(original)
        os.umask(mask)


@contextmanager
def refuse_lock(engine, key, mode, conflict):
    if mode:
        with folder_mode(engine, mode) as sentence:
            result = locks.acquire_target_lock(engine, key)
            assert (result.status, result.reason, result.message) == ('uncertain', 'folder_permissions', sentence)
            yield sentence
    else:
        result = locks.acquire_target_lock(engine, key)
        assert result.status == 'acquired'
        try:
            yield conflict
        finally:
            assert result.handle.close().released


def target_key(db, printer_id):
    from core.auth import SECRET_KEY
    from modules.printers.dispatch import _get_printer_info
    return build_identity(_get_printer_info(printer_id, connection=db), SECRET_KEY.encode()).target_key


@pytest.mark.parametrize('mode', [0o775, 0o777, None])
def test_dispatch_refusal_identifies_storage_or_conflict(setup, monkeypatch, mode):
    from modules.printers import dispatch
    engine, _ = setup
    no_devices(monkeypatch)
    conflict = 'A dispatch claim or target ownership conflict/uncertainty blocks dispatch; reconcile before retrying'
    with refuse_lock(engine, inputs(engine)['physical_key'], mode, conflict) as sentence:
        assert dispatch.dispatch_job(59, 53) == (False, sentence)


@pytest.mark.parametrize('mode', [0o775, 0o777, None])
def test_power_refusal_identifies_storage_or_conflict(runtime, token_client, review_db, mode):
    from modules.printers import routes_smart_plug
    _, engine, calls, _ = runtime
    client, issue = token_client
    client.app.include_router(routes_smart_plug.router)
    headers = issue(['write:printers'])
    key = target_key(review_db, 599)
    review_db.rollback()
    conflict = 'Power command was refused or its outcome is uncertain; reconcile before retrying.'
    with refuse_lock(engine, key, mode, conflict) as sentence:
        response = client.post('/printers/599/plug/on', headers=headers)
        assert response.status_code == 409
        detail = response.json()['detail']
        assert (detail['message'] if mode else detail) == sentence
    assert calls == []


@pytest.mark.parametrize('mode', [0o775, 0o777, None])
def test_reorder_refusal_identifies_storage_or_conflict(queued, mode):
    with refuse_lock(queued.get_bind(), target_key(queued, 9), mode,
                     'Printer queue is busy or target locking is unavailable') as sentence:
        with pytest.raises(OdinError) as exc:
            order(queued, [42, 41, 13])
        assert exc.value.status == 409 and exc.value.detail == sentence


@pytest.mark.parametrize('mode', [0o775, None])
def test_profile_refusal_identifies_storage_or_conflict(profile_client, review_db, mode):
    client, issue, calls, _ = profile_client
    headers = issue(['read:models', 'write:printers'])
    with refuse_lock(review_db.get_bind(), target_key(review_db, 9), mode,
                     'Profile target is busy or uncertain; reconcile before retrying') as sentence:
        response = client.post('/profiles/101/apply', headers=headers, json={'printer_id': 9})
        assert response.status_code == 409 and response.json()['detail'] == sentence
    assert calls == []


@pytest.mark.parametrize('mode', [0o775, None])
def test_recovery_refusal_identifies_storage_or_conflict(actions, review_db, mode):
    _, _, calls, _, post = actions
    assert post('hold').status_code == 200
    key = review_db.execute(text('SELECT physical_key FROM printer_dispatch_claims')).scalar_one()
    review_db.rollback()
    conflict = 'Dispatch recovery requires refreshed evidence or authorized investigation.'
    with refuse_lock(review_db.get_bind(), key, mode, conflict) as sentence:
        response = post('release')
        detail = response.json()['detail']
        assert response.status_code == 409 and detail['message'] == sentence
        assert detail['code'] == ('evidence_unavailable' if mode else 'target_busy')
    assert calls == []


@pytest.mark.parametrize('mode', [0o775, None])
def test_retirement_keeps_claim_and_reports_storage_cause(observed, caplog, mode):
    from core.dispatch_claims import retire_observed_terminal
    from core.dispatch_observations import bind_generic_observation
    engine, key = observed
    assert bind_generic_observation(engine, printer_id=59, print_job_id=70, key=key) == 'matched'
    physical = inputs(engine)['physical_key']
    with refuse_lock(engine, physical, mode, 'Another sender is using this printer. ODIN kept the print reservation.') as sentence:
        with caplog.at_level(logging.INFO, logger='core.dispatch_claims'):
            assert retire_observed_terminal(engine, printer_id=59, print_job_id=70) == 'retained'
        messages = [r.getMessage() for r in caplog.records if r.name == 'core.dispatch_claims']
        assert messages == [sentence]
    with engine.connect() as conn:
        assert conn.execute(text('SELECT job_id FROM printer_dispatch_claims')).scalar_one() == 53


def test_system_check_clears_when_folder_is_fixed(review_db, monkeypatch, tmp_path):
    from modules.system import troubleshooting
    from modules.organizations import services
    monkeypatch.setattr(services, 'upload_storage_probe', lambda: (tmp_path / 'missing', {'status': 'ready'}))
    monkeypatch.setattr(troubleshooting.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout='api RUNNING', returncode=0))
    def items():
        return [row for row in troubleshooting.server_checks(review_db)['checks'] if row['stage'] == 'data_folder_lock']
    with folder_mode(review_db.get_bind(), 0o777) as sentence:
        assert items() == [troubleshooting.check('data_folder_lock', 'warn', sentence)]
    assert items() == []


@pytest.mark.parametrize('unsafe', ['data_folder', 'lock_folder', 'lock_file'])
def test_shared_mount_checks_permissions_before_probing(engine, monkeypatch, unsafe):
    monkeypatch.setattr(locks, '_filesystem_type', lambda fd: 0x65735546)
    monkeypatch.setattr(locks, '_prove_exclusion', lambda fd: pytest.fail('Probed unsafe storage'))
    folder = Path(engine.url.database + '.odin-sender-locks')
    folder.mkdir(mode=0o700)
    lock_file = folder / (KEY + '.lock')
    lock_file.touch(mode=0o600)
    if unsafe == 'data_folder':
        folder.parent.chmod(0o775)
    elif unsafe == 'lock_folder':
        folder.chmod(0o755)
    else:
        lock_file.chmod(0o644)
    result = locks.acquire_target_lock(engine, KEY)
    assert (result.status, result.reason) == ('uncertain', 'folder_permissions' if unsafe == 'data_folder' else 'lock_permissions')


@pytest.mark.parametrize('item', ['database', 'data_folder', 'lock_folder', 'lock_file'])
def test_wrong_owner_names_item_and_required_uid_without_host_path(engine, monkeypatch, item):
    import stat
    db = Path(engine.url.database)
    folder = Path(str(db) + '.odin-sender-locks')
    folder.mkdir(mode=0o700)
    lock_file = folder / (KEY + '.lock')
    lock_file.touch(mode=0o600)
    path = {'database': db, 'data_folder': db.parent, 'lock_folder': folder, 'lock_file': lock_file}[item]
    inode = path.stat().st_ino
    def foreign(value):
        if value.st_ino != inode:
            return value
        fields = list(value)
        fields[stat.ST_UID] = os.geteuid() + 1
        return os.stat_result(fields)
    real_fstat, real_database = os.fstat, locks._database_stat
    monkeypatch.setattr(locks.os, 'fstat', lambda fd: foreign(real_fstat(fd)))
    monkeypatch.setattr(locks, '_database_stat', lambda fd, name: foreign(real_database(fd, name)))
    result = locks.acquire_target_lock(engine, KEY)
    expected = 'folder_owner' if item.endswith('folder') else 'file_owner'
    assert (result.status, result.reason) == ('uncertain', expected)
    assert f'UID {os.geteuid()}' in result.message
    assert str(db.parent) not in result.message
    assert ('or root' in result.message) == (item == 'data_folder')


def test_startup_logs_storage_warning_once_and_continues(engine, monkeypatch, caplog):
    import asyncio
    import faulthandler
    from core import app as application, db, job_timeline
    class ReachedNextStartupStep(Exception):
        pass
    def reached(*args):
        raise ReachedNextStartupStep
    monkeypatch.setattr(db, 'engine', engine)
    monkeypatch.setattr(application, '_discover_modules', lambda: [])
    monkeypatch.setattr(application, '_check_schema_drift', lambda *args: None)
    monkeypatch.setattr(faulthandler, 'register', lambda *args, **kw: None)
    monkeypatch.setattr(job_timeline, 'start', reached)
    app = application.create_app()
    async def start():
        async with app.router.lifespan_context(app):
            pass
    with folder_mode(engine, 0o775) as sentence:
        with caplog.at_level(logging.WARNING), pytest.raises(ReachedNextStartupStep):
            asyncio.run(start())
    assert [record.getMessage() for record in caplog.records].count(sentence) == 1


@pytest.mark.parametrize('mode', [0o775, None])
def test_automatic_power_logs_refusal_without_sending(runtime, review_db, caplog, mode):
    module, engine, calls, _ = runtime
    with refuse_lock(engine, target_key(review_db, 599), mode, 'Another sender is using this printer. No power command was sent.') as sentence:
        with caplog.at_level(logging.INFO, logger=module.log.name):
            module.on_print_start(599, print_job_id=599)
        assert sentence in [record.getMessage() for record in caplog.records]
    assert calls == []


@pytest.mark.parametrize('separate', ['lock_folder', 'lock_file'])
def test_parent_filesystem_cannot_vouch_for_separately_mounted_locks(engine, monkeypatch, separate):
    import stat
    directory = Path(engine.url.database + '.odin-sender-locks')
    directory.mkdir(mode=0o700)
    path = directory / (KEY + '.lock')
    path.touch(mode=0o600)
    inode = (directory if separate == 'lock_folder' else path).stat().st_ino
    real = os.fstat
    def different_device(fd):
        value = real(fd)
        if value.st_ino == inode:
            fields = list(value)
            fields[stat.ST_DEV] += 1
            return os.stat_result(fields)
        return value
    monkeypatch.setattr(locks.os, 'fstat', different_device)
    result = locks.acquire_target_lock(engine, KEY)
    assert (result.status, result.reason) == ('unsupported', 'filesystem')
    assert 'same local disk or Docker volume' in result.message
