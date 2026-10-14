"""Nonce-scoped frame directory identity; caller owns DB transaction/media guard."""
import os
import re
import stat
import uuid
from sqlalchemy import select, text
from core.timelapse_ownership_schema import anchors
from core.timelapse_publication_schema import lifetimes, versions, namespaces, bindings


class FrameNamespaceUnavailable(ValueError):
    pass


def _refuse():
    raise FrameNamespaceUnavailable('Frame directory authority could not be confirmed') from None


def _identity(value):
    return dict(device=value.st_dev, inode=value.st_ino, uid=value.st_uid,
                gid=value.st_gid, mode=value.st_mode)


def bind_or_open_namespace(db, root_fd, root_identity, guard, *, nonce, timelapse_id, printer_id, job_id):
    """Return a private descriptor only for the exact current capture lifetime.

    Caller must hold the root-wide media guard through commit and descriptor use.
    Creation may leave an empty, unbound directory after rollback; it is never
    adopted on retry. This primitive does not publish frames or grant camera access.
    """
    descriptor = None
    try:
        if (type(nonce) is not str or re.fullmatch('[0-9a-f]{32}', nonce) is None
                or any(type(value) is not int or value <= 0 for value in (timelapse_id, printer_id, job_id))):
            _refuse()
        guard.assert_owned(); guard.assert_root_identity(root_identity)
        actual_root = _identity(os.fstat(root_fd))
        expected_root = {name: getattr(root_identity, name) for name in actual_root}
        if actual_root != expected_root:
            _refuse()
        # Same portable write-lock ordering used by capture creation.
        for table, identifier in (('jobs', job_id), ('printers', printer_id), ('timelapses', timelapse_id)):
            if db.get_bind().dialect.name == 'postgresql':
                result = db.execute(text(f'SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT'), {'id': identifier})
                if result.first() is None: _refuse()
            else:
                if db.execute(text(f'UPDATE {table} SET id=id WHERE id=:id'), {'id': identifier}).rowcount != 1:
                    _refuse()
        row = db.execute(text('SELECT printer_id,print_job_id,filename,status,ownership_operation_id,retirement_operation_id '
                              'FROM timelapses WHERE id=:id'), {'id': timelapse_id}).mappings().one()
        owner = db.execute(select(anchors).where(anchors.c.operation_id == nonce)).mappings().one()
        job = db.execute(text('SELECT printer_id,status FROM jobs WHERE id=:id'), {'id': job_id}).mappings().one()
        if (row['printer_id'] != printer_id or row['print_job_id'] != job_id or row['status'] != 'capturing'
                or row['ownership_operation_id'] != nonce or row['retirement_operation_id'] is not None
                or owner['timelapse_id'] != timelapse_id or owner['printer_id'] != printer_id
                or owner['scheduled_job_id'] != job_id or owner['filename'] != row['filename']
                or owner['owner_state'] not in ('known', 'shared') or owner['ownership_source'] != 'first_capture_job_file'
                or job['printer_id'] != printer_id or job['status'] != 'printing'):
            _refuse()
        lifetime = db.execute(select(lifetimes).where(lifetimes.c.lifetime_nonce == nonce)).mappings().one_or_none()
        binding = db.execute(select(bindings).where(bindings.c.lifetime_nonce == nonce)).mappings().one_or_none()
        if lifetime is None:
            if binding is not None:
                _refuse()
            # Existing numeric-ID media has no recorded lifetime identity.
            try: os.stat(str(timelapse_id), dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError: pass
            else: _refuse()
            db.execute(lifetimes.insert().values(lifetime_nonce=nonce, original_timelapse_id=timelapse_id,
                original_printer_id=printer_id, **{'root_'+name: value for name,value in actual_root.items()}))
            db.execute(versions.insert().values(version_nonce=nonce, lifetime_nonce=nonce))
        elif (lifetime['original_timelapse_id'] != timelapse_id or lifetime['original_printer_id'] != printer_id
              or any(lifetime['root_'+name] != value for name,value in actual_root.items())):
            _refuse()
        if binding is None:
            namespace_id = uuid.uuid4().hex
            relative = 'frames_' + namespace_id
            os.mkdir(relative, mode=0o700, dir_fd=root_fd)
            descriptor = os.open(relative, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            facts = _identity(os.fstat(descriptor))
            if facts['mode'] != stat.S_IFDIR | 0o700 or facts['device'] != actual_root['device'] or facts['uid'] != actual_root['uid'] or facts['gid'] != actual_root['gid']:
                _refuse()
            os.fsync(descriptor)
            os.fsync(root_fd)
            db.execute(namespaces.insert().values(namespace_id=namespace_id, lifetime_nonce=nonce,
                relative_path=relative, phase='bound', **facts))
            db.execute(bindings.insert().values(lifetime_nonce=nonce, namespace_id=namespace_id, next_number=1))
        else:
            record = db.execute(select(namespaces).where(namespaces.c.namespace_id == binding['namespace_id'])).mappings().one()
            relative = record['relative_path']
            if (record['phase'] != 'bound' or record['lifetime_nonce'] != nonce
                    or type(relative) is not str or relative != 'frames_' + record['namespace_id']):
                _refuse()
            descriptor = os.open(relative, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            if any(record[name] != value for name,value in _identity(os.fstat(descriptor)).items()):
                _refuse()
        from core.timelapse_publication_validation import bind as validate_binding
        validate_binding(db, db.execute(select(bindings).where(bindings.c.lifetime_nonce == nonce)).mappings().one())
        current = os.stat(relative, dir_fd=root_fd, follow_symlinks=False)
        if _identity(current) != _identity(os.fstat(descriptor)):
            _refuse()
        guard.assert_owned()
        result, descriptor = descriptor, None
        return result
    except Exception:
        _refuse()
    finally:
        if descriptor is not None:
            try: os.close(descriptor)
            except OSError: _refuse()
