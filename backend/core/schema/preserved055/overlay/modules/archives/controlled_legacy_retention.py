"""Retire attributable pre-anchor media under the shared root writer guard.

No ownership anchor is invented. Only terminal legacy rows with a surviving
controlled job qualify. Ambiguous/shared paths, links and retained lifetime
history are refused. Missing files permit an interrupted purge to resume.
"""
import os
import stat
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.timelapse_media_lock import acquire_media_guard
from core.controlled_retention import expired, preview_acknowledged
from core.audit import log_audit


def purge_legacy(engine, root, identifier):
    root = Path(root)
    with acquire_media_guard(root, identifier), Session(engine) as db:
        suffix = ' FOR UPDATE' if db.get_bind().dialect.name == 'postgresql' else ''
        if not suffix:
            db.execute(text('UPDATE timelapses SET id=id WHERE id=:id'), {'id': identifier})
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- retained-media predicate uses a fixed alias and source-owned SQL; media id is bound
        row = db.execute(text('SELECT filename,ownership_operation_id,retirement_operation_id '
                              'FROM timelapses WHERE id=:id' + suffix), {'id': identifier}).first()
        if not row or row.ownership_operation_id or row.retirement_operation_id:
            return False
        if not expired(db, identifier) or not preview_acknowledged(db, identifier):
            return False
        name = row.filename
        if not name or Path(name).name != name or name in ('.', '..') or '\\' in name:
            raise ValueError('controlled_legacy_unsafe_path')
        collision = db.execute(text('SELECT 1 FROM timelapses WHERE filename=:name AND id<>:id '
            'UNION ALL SELECT 1 FROM timelapse_ownership_anchors WHERE filename=:name OR timelapse_id=:id '
            'UNION ALL SELECT 1 FROM timelapse_retirements WHERE filename=:name OR timelapse_id=:id'),
            {'name': name, 'id': identifier}).first()
        if collision:
            raise ValueError('controlled_legacy_shared_path')
        paths = []
        def collect(path, depth=0):
            if len(paths) >= 16384 or depth > 32:
                raise ValueError('controlled_legacy_manifest_limit')
            try:
                facts = path.lstat()
            except FileNotFoundError:
                return
            if stat.S_ISREG(facts.st_mode) and facts.st_nlink == 1:
                paths.append((path, facts, False))
            elif stat.S_ISDIR(facts.st_mode):
                for child in path.iterdir():
                    collect(child, depth + 1)
                paths.append((path, facts, True))
            else:
                raise ValueError('controlled_legacy_unsafe_path')
        collect(root / name)
        collect(root / str(identifier))
        # Verify the complete manifest before the first unlink; the guard excludes
        # application writers and row locks serialize policy/deletion changes.
        for path, facts, _ in paths:
            current = path.lstat()
            if (current.st_dev, current.st_ino, current.st_mode, current.st_size) != (facts.st_dev, facts.st_ino, facts.st_mode, facts.st_size):
                raise ValueError('controlled_legacy_object_changed')
        for path, _, directory in paths:
            path.rmdir() if directory else path.unlink()
        db.execute(text('DELETE FROM timelapses WHERE id=:id'), {'id': identifier})
        log_audit(db, 'controlled_part.timelapse_purged', 'timelapse', identifier, {'legacy': True})
        db.commit()
        return True
