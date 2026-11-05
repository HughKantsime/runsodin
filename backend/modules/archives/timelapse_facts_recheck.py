"""Explicit admin repair of restart-only published ctime drift; never a boot hook."""
from contextlib import ExitStack
import hashlib
import os

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from core.audit import log_audit
from core.timelapse_manifest import _root, _parent, _parts, _facts
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_publication_schema import publications, facts
from core.timelapse_request_budget import VerificationBudget
from modules.archives.timelapse_action_authority import fence_authorization, fresh_action_principal, lock_media_sources
from modules.archives.timelapse_read_lease import _snapshot

FILE_KEYS = ('device', 'inode', 'uid', 'gid', 'mode', 'link_count', 'byte_count', 'mtime_ns')
STABLE_KEYS = ('device', 'inode', 'uid', 'gid', 'mode')


class RecheckRefused(ValueError):
    """Fixed reason codes, safe for the operator report."""


def require_admin(db, evidence):
    principal = fresh_action_principal(db, evidence)
    if evidence.purpose != 'read' or principal['role'] != 'admin' or principal.get('group_id') is not None:
        raise RecheckRefused('platform_admin_required')


def _source_references(db, snapshot, *, lock=False):
    """Completed speed copies bind this current video's ctime in their source fact."""
    nonce = snapshot['media'][4]
    ops = [dict(row) for row in db.execute(select(publications).where(
        publications.c.source_nonce == nonce, publications.c.kind != 'frame',
    ).order_by(publications.c.operation_id).limit(16385)).mappings()]
    if len(ops) > 16384:
        raise RecheckRefused('source_reference_limit')
    rows = []
    for op in ops:
        if op['kind'] != 'speed' or op['phase'] != 'complete' or op['state'] != 'complete':
            raise RecheckRefused('dependent_writer_active')
        if op['source_filename'] != snapshot['media'][3] or op['source_lifetime'] != snapshot['life']['lifetime_nonce']:
            raise RecheckRefused('source_reference_changed')
        source = [dict(row) for row in db.execute(select(facts).where(
            facts.c.operation_id == op['operation_id'], facts.c.role == 'source',
        ).limit(2)).mappings()]
        if len(source) != 1 or source[0]['relative_path'] != snapshot['media'][3]:
            raise RecheckRefused('source_reference_changed')
        row = source[0]
        if (any(row[key] != snapshot['output'][key] for key in (*FILE_KEYS, 'kind', 'digest_sha256'))
                or row['ctime_ns'] != snapshot['op']['published_ctime_ns']):
            raise RecheckRefused('source_reference_facts_differ')
        rows.append(row)
    if lock:
        lock_media_sources(db, ((publications, ops), (facts, rows)))
    return ops, rows


def recheck_timelapse(engine, root, tid, evidence, *, budget=None):
    """Verify a ready video under the reader's fences; atomically audit its refresh.

    Output facts describe the pre-rename file and remain immutable. The effective
    current ctime used by readers/editors is publications.published_ctime_ns.
    Completed speed source receipts for this same inode refresh atomically too.
    Pre-rename output, frame, private and retirement facts are never rewritten.
    """
    budget = budget if budget is not None else VerificationBudget()
    with acquire_media_guard(root, tid) as guard, ExitStack() as stack:
        root_fd, identity = stack.enter_context(_root(root))
        with Session(engine) as db:
            fence_authorization(db)
            require_admin(db, evidence)
            before = _snapshot(db, evidence, tid)
            references = _source_references(db, before)
        if any(getattr(identity, key) != before['life']['root_' + key] for key in STABLE_KEYS):
            raise RecheckRefused('root_changed')
        guard.assert_root_identity(identity)
        path = before['media'][3]
        parts = _parts(path)
        known = {row['relative_path']: row for row in before['facts'] if row['role'] == 'output_parent'}
        if any('/'.join(parts[:i]) not in known for i in range(1, len(parts))):
            raise RecheckRefused('parent_facts_missing')
        parents = {}
        parent, name = stack.enter_context(_parent(root_fd, parts, identity, parents))
        for relative, value in parents.items():
            if any(getattr(value, key) != known[relative][key] for key in STABLE_KEYS):
                raise RecheckRefused('parent_changed')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        stack.callback(os.close, fd)
        value = _facts(path, os.fstat(fd))
        output = before['output']
        differences = [key for key in FILE_KEYS if getattr(value, key) != output[key]]
        if value.kind != output['kind']:
            differences.append('kind')
        if differences:
            raise RecheckRefused('facts_differ:' + ','.join(differences))
        budget.consume(value.byte_count)
        digest = hashlib.sha256()
        position = 0
        while position < value.byte_count:
            budget.check()
            chunk = os.pread(fd, min(1 << 20, value.byte_count - position), position)
            if not chunk:
                raise RecheckRefused('file_changed_during_verification')
            digest.update(chunk)
            position += len(chunk)
        if digest.hexdigest() != output['digest_sha256']:
            raise RecheckRefused('digest_mismatch')
        with Session(engine) as db:
            fence_authorization(db, preparation=True)
            require_admin(db, evidence)
            if _snapshot(db, evidence, tid, lock=True) != before:
                raise RecheckRefused('publication_changed')
            if _source_references(db, before, lock=True) != references:
                raise RecheckRefused('source_reference_changed')
            budget.check()
            guard.assert_root_identity(identity)
            if _facts(path, os.fstat(fd)) != value or _facts(path, os.stat(name, dir_fd=parent, follow_symlinks=False)) != value:
                raise RecheckRefused('file_changed_during_verification')
            with _root(root) as (_, current_root):
                if any(getattr(current_root, key) != getattr(identity, key) for key in STABLE_KEYS):
                    raise RecheckRefused('root_changed')
                with _parent(root_fd, parts, identity, dict(parents)) as (check_parent, check_name):
                    if _facts(path, os.stat(check_name, dir_fd=check_parent, follow_symlinks=False)) != value:
                        raise RecheckRefused('file_changed_during_verification')
            old = before['op']['published_ctime_ns']
            if old == value.ctime_ns:
                return {'timelapse_id': tid, 'status': 'unchanged'}
            op = before['op']
            result = db.execute(update(publications).where(
                publications.c.operation_id == op['operation_id'],
                publications.c.revision == op['revision'],
                publications.c.published_ctime_ns == old,
                publications.c.phase == 'complete', publications.c.state == 'complete',
            ).values(published_ctime_ns=value.ctime_ns, revision=op['revision'] + 1))
            if result.rowcount != 1:
                raise RecheckRefused('publication_changed')
            for dependent, source in zip(*references):
                changed = db.execute(update(facts).where(
                    facts.c.operation_id == source['operation_id'], facts.c.role == 'source',
                    facts.c.relative_path == source['relative_path'], facts.c.ctime_ns == old,
                ).values(ctime_ns=value.ctime_ns))
                revised = db.execute(update(publications).where(
                    publications.c.operation_id == dependent['operation_id'],
                    publications.c.revision == dependent['revision'],
                ).values(revision=dependent['revision'] + 1))
                if changed.rowcount != 1 or revised.rowcount != 1:
                    raise RecheckRefused('source_reference_changed')
            details = {'timelapse_id': tid, 'old_ctime_ns': old, 'new_ctime_ns': value.ctime_ns}
            log_audit(db, 'timelapse.facts_reverified', 'timelapse', tid, details)
            db.flush()  # Audit failures roll back the ctime update as well.
            guard.assert_root_identity(identity)
            if _facts(path, os.fstat(fd)) != value or _facts(path, os.stat(name, dir_fd=parent, follow_symlinks=False)) != value:
                raise RecheckRefused('file_changed_during_verification')
            with _parent(root_fd, parts, identity, dict(parents)) as (check_parent, check_name):
                if _facts(path, os.stat(check_name, dir_fd=check_parent, follow_symlinks=False)) != value:
                    raise RecheckRefused('file_changed_during_verification')
            db.commit()
            return {**details, 'status': 'reverified'}


def recheck_facts(engine, root, evidence, *, timelapse_ids=None, after_id=0):
    """One bounded page; each successful repair commits its own audit row."""
    from core.timelapse_media_errors import RetirementUnavailable
    from core.timelapse_media_lock import MediaGuardUnavailable
    from modules.archives.models import Timelapse

    if timelapse_ids is not None and not 1 <= len(timelapse_ids) <= 100:
        raise RecheckRefused('selection_limit')
    with Session(engine) as db:
        fence_authorization(db)
        require_admin(db, evidence)
        candidates = (list(db.scalars(select(Timelapse.id).where(
            Timelapse.status == 'ready', Timelapse.id > after_id,
        ).order_by(Timelapse.id).limit(101))) if timelapse_ids is None
            else list(dict.fromkeys(timelapse_ids)))
    ids = candidates[:100]
    budget = VerificationBudget()
    results = []
    for tid in ids:
        try:
            result = recheck_timelapse(engine, root, tid, evidence, budget=budget)
            results.append({'timelapse_id': tid, 'status': 're-admitted',
                            'changed': result['status'] == 'reverified'})
        except Exception as exc:
            # Never return SQL, credentials or filesystem paths from unexpected errors.
            reason = (str(exc) if isinstance(exc, RecheckRefused) else
                      exc.reason if isinstance(exc, RetirementUnavailable) else
                      'writer_active' if isinstance(exc, MediaGuardUnavailable) else
                      'admission_or_io_refused')
            results.append({'timelapse_id': tid, 'status': 'refused', 'reason': reason})
    return {'results': results, 'next_after_id': ids[-1] if len(candidates) > 100 else None}
