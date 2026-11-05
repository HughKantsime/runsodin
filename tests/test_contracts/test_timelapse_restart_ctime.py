"""Real shell ownership step and audited ready-video repair on owned SQLite/FDs."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.orm import Session
from tests.test_contracts.test_timelapse_encoder_publication import encoder, encode, frame_writer, review_db, owned_runtime
from tests.test_contracts.test_timelapse_lifetime_action import credential
from tests.test_contracts.test_timelapse_edit_publication import edit, run, prepared_source
from core.timelapse_publication_schema import publications, facts
from modules.archives import timelapse_facts_recheck as service


@pytest.mark.parametrize('step', [0, 1])
def test_entrypoint_ownership_preserves_ctime_and_repairs_wrong_group(tmp_path, step):
    source = (Path(__file__).resolve().parents[2] / 'docker/entrypoint.sh').read_text()
    lines = [line for line in source.splitlines() if '/data' in line and 'chown' in line and not line.startswith('#')]
    assert len(lines) == 2
    uid, gid = os.getuid(), os.getgid()
    alternate = next(group for group in os.getgroups() if group != gid)
    owned, wrong = tmp_path / 'already owned', tmp_path / 'needs ownership'
    owned.write_bytes(b'unchanged'); wrong.write_bytes(b'hand to application')
    os.chown(wrong, uid, alternate)
    assert wrong.stat().st_gid != gid
    before = owned.stat().st_ctime_ns
    time.sleep(.01)
    command = (lines[step].replace('/data', shlex.quote(str(tmp_path)))
               .replace('odin:odin', f'{uid}:{gid}')
               .replace('-user odin', f'-user {uid}').replace('-group odin', f'-group {gid}'))
    subprocess.run(['/bin/sh', '-c', command], check=True)
    assert owned.stat().st_ctime_ns == before
    assert (wrong.stat().st_uid, wrong.stat().st_gid) == (uid, gid)
    assert wrong.read_bytes() == b'hand to application'


@pytest.fixture
def ready(encoder, review_db):
    assert encode(encoder)
    _, tid, root, _, _, _ = encoder
    engine = review_db.get_bind()
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.commit()
    def fk(connection, record, proxy):
        connection.execute('PRAGMA foreign_keys=ON')
    event.listen(engine, 'checkout', fk)
    evidence = replace(credential(engine), purpose='read')
    with Session(engine) as db:
        filename = db.execute(text('SELECT filename FROM timelapses WHERE id=:id'), {'id': tid}).scalar_one()
    path = root / filename
    before = path.stat().st_ctime_ns
    time.sleep(.01)
    os.chmod(path, path.stat().st_mode)  # Same metadata, but a real ctime change.
    assert before != path.stat().st_ctime_ns
    yield engine, root, tid, evidence, path
    evidence.scope.clear()
    event.remove(engine, 'checkout', fk)


def journal(engine):
    with Session(engine) as db:
        return (list(db.execute(select(publications).order_by(publications.c.operation_id))),
                list(db.execute(select(facts).order_by(*facts.primary_key.columns))),
                list(db.execute(text("SELECT * FROM audit_logs WHERE action='timelapse.facts_reverified' ORDER BY id"))))


def test_recheck_restores_reader_and_audits_exactly_once(ready):
    engine, root, tid, evidence, path = ready
    from modules.archives.timelapse_read_lease import admitted_response
    from modules.archives.timelapse_retirement import RetirementUnavailable
    with pytest.raises(RetirementUnavailable):
        admitted_response(engine, root, tid, evidence)
    before = journal(engine)
    result = service.recheck_timelapse(engine, root, tid, evidence)
    assert result['status'] == 'reverified' and result['new_ctime_ns'] == path.stat().st_ctime_ns
    after = journal(engine)
    assert after[1] == before[1]  # Historical pre-rename and frame facts remain intact.
    assert len(after[2]) == 1
    row = after[2][0]._mapping
    details = json.loads(row['details']) if isinstance(row['details'], str) else row['details']
    assert details == {key: result[key] for key in ('timelapse_id', 'old_ctime_ns', 'new_ctime_ns')}
    assert row['entity_id'] == tid and row['actor_user_id'] == 3
    response = admitted_response(engine, root, tid, evidence)
    try:
        assert response.status_code == 200 and os.pread(response.fd, 1000, 0) == path.read_bytes()
    finally:
        response.close()
    assert service.recheck_timelapse(engine, root, tid, evidence)['status'] == 'unchanged'
    assert journal(engine) == after


def test_same_size_content_change_with_restored_mtime_refuses(ready):
    engine, root, tid, evidence, path = ready
    original = path.stat()
    path.write_bytes(b'x' * original.st_size)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    before = journal(engine)
    with pytest.raises(service.RecheckRefused, match='digest_mismatch'):
        service.recheck_timelapse(engine, root, tid, evidence)
    assert journal(engine) == before


@pytest.mark.parametrize('key', service.FILE_KEYS)
def test_every_other_stored_fact_difference_refuses(ready, key):
    engine, root, tid, evidence, path = ready
    with engine.begin() as db:
        op = db.execute(select(publications.c.operation_id).where(publications.c.kind == 'encode')).scalar_one()
        # Valid but different facts; link_count's schema enforces 1, so change the real inode instead.
        if key == 'link_count':
            os.link(path, path.parent / 'extra-link')
        else:
            delta = 64 if key == 'mode' else 1
            db.execute(update(facts).where(facts.c.operation_id == op, facts.c.role == 'output').values({key: facts.c[key] + delta}))
    before = journal(engine)
    with pytest.raises(service.RecheckRefused, match=key):
        service.recheck_timelapse(engine, root, tid, evidence)
    assert journal(engine) == before


@pytest.mark.parametrize('change', ['role', 'org', 'inactive', 'status', 'unowned'])
def test_authority_or_ready_state_required(ready, change):
    engine, root, tid, evidence, _ = ready
    sql = {'role': "UPDATE users SET role='operator' WHERE id=3", 'org': 'UPDATE users SET group_id=1 WHERE id=3',
           'inactive': 'UPDATE users SET is_active=0 WHERE id=3', 'status': "UPDATE timelapses SET status='failed'",
           'unowned': 'UPDATE timelapses SET ownership_operation_id=NULL'}[change]
    with engine.begin() as db: db.execute(text(sql))
    before = journal(engine)
    with pytest.raises(Exception): service.recheck_timelapse(engine, root, tid, evidence)
    assert journal(engine) == before


def test_audit_insert_failure_rolls_back_refresh(ready):
    engine, root, tid, evidence, _ = ready
    before = journal(engine)
    def fail(conn, cursor, statement, parameters, context, many):
        if statement.startswith('INSERT INTO audit_logs'): raise RuntimeError('owned audit failure')
    event.listen(engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='owned audit failure'):
            service.recheck_timelapse(engine, root, tid, evidence)
    finally: event.remove(engine, 'before_cursor_execute', fail)
    assert journal(engine) == before


@pytest.mark.parametrize('change', ['bytes', 'path', 'revocation', 'revision'])
def test_change_during_digest_refuses_without_writes(ready, monkeypatch, change):
    engine, root, tid, evidence, path = ready
    original = os.pread
    fired = []
    def read(fd, size, offset):
        data = original(fd, size, offset)
        if not fired:
            fired.append(True)
            if change == 'bytes': path.write_bytes(b'x' * path.stat().st_size)
            elif change == 'path':
                path.rename(path.with_suffix('.saved')); path.write_bytes(data)
            else:
                with engine.begin() as db:
                    db.execute(text('UPDATE users SET is_active=0 WHERE id=3' if change == 'revocation' else
                                    "UPDATE timelapse_publications SET revision=revision+1 WHERE kind='encode'"))
        return data
    with Session(engine) as db:
        old_ctime = db.execute(select(publications.c.published_ctime_ns).where(publications.c.kind == 'encode')).scalar_one()
    monkeypatch.setattr(os, 'pread', read)
    with pytest.raises(Exception): service.recheck_timelapse(engine, root, tid, evidence)
    with Session(engine) as db:
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='timelapse.facts_reverified'")).scalar_one() == 0
        assert db.execute(select(publications.c.published_ctime_ns).where(publications.c.kind == 'encode')).scalar_one() == old_ctime


def test_media_lock_is_required(ready):
    from core.timelapse_media_lock import acquire_media_guard, MediaGuardUnavailable
    engine, root, tid, evidence, _ = ready
    before = journal(engine)
    with acquire_media_guard(root, tid):
        with pytest.raises(MediaGuardUnavailable): service.recheck_timelapse(engine, root, tid, evidence)
    assert journal(engine) == before


def test_file_changed_by_audit_flush_rolls_back_everything(ready):
    engine, root, tid, evidence, path = ready
    before = journal(engine)
    def change(conn, cursor, statement, parameters, context, many):
        if statement.startswith('INSERT INTO audit_logs'):
            path.write_bytes(b'x' * path.stat().st_size)
    event.listen(engine, 'after_cursor_execute', change)
    try:
        with pytest.raises(service.RecheckRefused, match='file_changed_during_verification'):
            service.recheck_timelapse(engine, root, tid, evidence)
    finally:
        event.remove(engine, 'after_cursor_execute', change)
    assert journal(engine) == before


def test_entrypoint_never_invokes_recheck():
    source = (Path(__file__).resolve().parents[2] / 'docker/entrypoint.sh').read_text()
    assert 'recheck_timelapse_facts' not in source and 'timelapse_facts_recheck' not in source


@pytest.mark.parametrize('tamper', [False, True])
def test_speed_source_receipt_stays_consistent_or_refuses(edit, tamper):
    from core.timelapse_recipes import make_recipe
    from modules.archives.timelapse_speed_dependencies import completed_speed_source
    _, engine, root, tid, nonce, evidence = edit
    run(edit, recipe=make_recipe('speed', multiplier=2))
    with engine.begin() as db:
        db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
        filename = db.execute(text('SELECT filename FROM timelapses WHERE id=:id'), {'id': tid}).scalar_one()
        if tamper:
            db.execute(update(facts).where(facts.c.operation_id == 'a'*32, facts.c.role == 'source').values(mtime_ns=facts.c.mtime_ns + 1))
    def fk(connection, record, proxy): connection.execute('PRAGMA foreign_keys=ON')
    event.listen(engine, 'checkout', fk)
    try:
        path = root / filename
        time.sleep(.01); os.chmod(path, path.stat().st_mode)
        before = journal(engine)
        if tamper:
            with pytest.raises(service.RecheckRefused, match='source_reference_facts_differ'):
                service.recheck_timelapse(engine, root, tid, replace(evidence, purpose='read'))
            assert journal(engine) == before
        else:
            assert service.recheck_timelapse(engine, root, tid, replace(evidence, purpose='read'))['status'] == 'reverified'
            with Session(engine) as db:
                op = dict(db.execute(select(publications).where(publications.c.operation_id == 'a'*32)).mappings().one())
                proven = completed_speed_source(db, op)
                assert proven['ctime_ns'] == path.stat().st_ctime_ns
                assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='timelapse.facts_reverified'")).scalar_one() == 1
    finally:
        event.remove(engine, 'checkout', fk)
