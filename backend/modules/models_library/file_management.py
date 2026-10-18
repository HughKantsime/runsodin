"""Audience-aware generic file metadata and atomic queue lifecycle."""

from core.quota import charge_job_quota
from core.errors import OdinError
import json
import logging
from fastapi import HTTPException
from sqlalchemy import text
from core.dependencies import log_audit
from core.db_compat import execute_insert_returning_id
from core.model_preview_access import accessible_preview_file, is_education_owned_file, delete_generic_file_if_unchanged
from core.rbac import check_org_access
from core.registry import registry

log = logging.getLogger('odin.api')


def assert_generic_model_reference_allowed(db, model_id):
    if model_id is None:
        return
    policy = registry.get_provider('EducationPolicyProvider')
    if policy is None:
        raise HTTPException(503, 'Education policy unavailable')
    policy.assert_generic_model_reference_allowed(db, model_id)


def _file_metadata(row):
    data = dict(row._mapping)
    data['filaments'] = json.loads(data.pop('filaments_json') or '[]')
    data.pop('stored_path', None)
    seconds = data['print_time_seconds']
    data['print_time_formatted'] = (f'{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m' if seconds is not None and seconds >= 3600 else f'{int(seconds // 60)}m' if seconds is not None else None)
    return data


def read_generic_file(db, file_id, actor):
    return _file_metadata(accessible_preview_file(db, file_id, actor))


def list_generic_files(db, actor, limit, include_scheduled):
    files, offset = [], 0
    while len(files) < limit:
        predicate = '' if include_scheduled else ' WHERE job_id IS NULL'
        candidates = db.execute(text('SELECT id FROM print_files' + predicate + ' ORDER BY uploaded_at DESC,id DESC LIMIT 100 OFFSET :offset'), {'offset': offset}).fetchall()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- only a constant WHERE fragment is appended; values are bound
        if not candidates:
            break
        offset += len(candidates)
        for candidate in candidates:
            try:
                row = accessible_preview_file(db, candidate.id, actor)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                continue
            # Recheck the snapshot after the candidate query, not just its earlier filter.
            if not include_scheduled and row.job_id is not None:
                continue
            data = _file_metadata(row)
            data.update(job_status=None, job_name=None)
            if row.job_id is not None:
                job = db.execute(text('SELECT j.status,j.item_name,j.model_id,j.print_file_id,j.charged_to_org_id,EXISTS(SELECT 1 FROM education_submissions s WHERE s.job_id=j.id) AS education_owned FROM jobs j WHERE j.id=:id'), {'id': row.job_id}).first()
                if (job and check_org_access(actor, job.charged_to_org_id) and job.model_id == row.model_id
                        and job.print_file_id in (None, row.id)
                        and (not job.education_owned or db.execute(text('SELECT 1 FROM education_submissions WHERE job_id=:job AND print_file_id=:file AND model_id=:model'), {'job': row.job_id, 'file': row.id, 'model': row.model_id}).first())):
                    data.update(job_status=job.status, job_name=job.item_name)
            files.append(data)
            if len(files) == limit:
                break
    return files


def _generic_parent(db, row, actor):
    if row.model_id is None:
        return None
    parent = db.execute(text('SELECT id,org_id FROM models WHERE id=:id'), {'id': row.model_id}).first()
    if not parent or not check_org_access(actor, parent.org_id):
        raise HTTPException(404, 'Print file not found')
    return parent


def _assert_generic_payload(db, row):
    if is_education_owned_file(db, row.id, row.stored_path):
        raise HTTPException(409, 'Education files require their approval workflow')


def _rollback_file_transaction(db, file_id):
    try:
        db.rollback()
    except Exception:
        log.warning('Print file rollback failed for file %s', file_id)
        raise HTTPException(500, f'Print file outcome is uncertain. Contact support with file reference {file_id} before retrying.') from None


def delete_generic_file(db, file_id, actor):
    try:
        row = accessible_preview_file(db, file_id, actor)
        _assert_generic_payload(db, row)
        parent = _generic_parent(db, row, actor)
        if row.job_id is not None or db.execute(text('SELECT 1 FROM jobs WHERE print_file_id=:id LIMIT 1'), {'id': file_id}).first():
            raise HTTPException(409, 'Print file is referenced by a job')
        if not delete_generic_file_if_unchanged(db, row, parent_org_id=parent.org_id if parent else None, require_unreferenced=True):
            _rollback_file_transaction(db, file_id)
            raise HTTPException(409, 'Print file changed or is referenced; refresh before deleting')
        log_audit(db, 'print_file.deleted', 'print_file', file_id, {'model_id': row.model_id})
        db.commit()
        return {'deleted': True}
    except HTTPException:
        _rollback_file_transaction(db, file_id)
        raise
    except Exception as exc:
        _rollback_file_transaction(db, file_id)
        log.warning('Print file deletion failed for file %s (%s)', file_id, type(exc).__name__)
        raise HTTPException(500, 'Unable to delete print file. Refresh before retrying.') from None


def schedule_generic_file(db, file_id, printer_id, actor):
    from core.interfaces.queue_creation import require_queue_creation_provider
    queue_creator = require_queue_creation_provider()
    try:
        # Portable row serialization: competing schedulers re-read only after this write.
        db.execute(text('UPDATE print_files SET id=id WHERE id=:id'), {'id': file_id})
        row = accessible_preview_file(db, file_id, actor)
        _assert_generic_payload(db, row)
        if not row.stored_path:
            raise HTTPException(400, "Print file payload is unavailable")
        from modules.models_library.queue_payload import (
            FILE_FIELDS, TARGET_FIELDS, lock_queue_reference, capture,
            assert_unchanged, verified_queue_payload, assert_payload_unchanged,
            file_snapshot_predicate,
        )
        if row.model_id is not None:
            lock_queue_reference(db, 'models', row.model_id, ('id', 'org_id'))
        parent = _generic_parent(db, row, actor)
        assert_generic_model_reference_allowed(db, row.model_id)
        if row.job_id is not None:
            raise HTTPException(409, 'File already scheduled')
        target = None
        if printer_id is not None:
            target = lock_queue_reference(db, 'printers', printer_id, TARGET_FIELDS)
            if not target or not (check_org_access(actor, target.org_id) or target.shared):
                raise HTTPException(404, 'Printer not found')
        file_capture = capture(row, FILE_FIELDS)
        parent_capture = capture(parent, ('id', 'org_id'))
        target_capture = capture(target, TARGET_FIELDS)
        with verified_queue_payload(row, target) as payload_hash:
            assert_unchanged(db, 'print_files', file_capture)
            assert_unchanged(db, 'models', parent_capture)
            assert_unchanged(db, 'printers', target_capture)
        filaments = json.loads(row.filaments_json or '[]')
        charge_job_quota(db, actor)

        job = queue_creator.stage_values(db, {
            "item_name": row.project_name or row.original_filename or row.filename,
            "model_id": row.model_id, "print_file_id": row.id,
            "payload_sha256": payload_hash, "plate_index": 0,
            "duration_hours": row.print_time_seconds / 3600 if row.print_time_seconds is not None else None,
            "colors_required": ','.join(f['color'] for f in filaments if f.get('color')),
            "quantity": 1, "priority": 5, "status": "pending", "printer_id": printer_id,
            **({"target_type": "specific", "target_filter": str(printer_id)} if printer_id is not None else {}),
            "hold": False, "is_locked": False, "submitted_by": actor['id'],
            "charged_to_user_id": actor['id'], "charged_to_org_id": actor.get('group_id'),
        }, actor)
        job_id = job.id
        assert_unchanged(db, 'print_files', file_capture)
        assert_unchanged(db, 'models', parent_capture)
        assert_unchanged(db, 'printers', target_capture)
        _assert_generic_payload(db, row)
        assert_generic_model_reference_allowed(db, row.model_id)
        assert_payload_unchanged(row.stored_path, payload_hash)
        capture_predicate, capture_parameters = file_snapshot_predicate(file_capture)
        linked = db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- columns checked against the FILE_FIELDS allowlist in file_snapshot_predicate; values are bound
            f'''UPDATE print_files SET job_id=:job WHERE id=:id AND job_id IS NULL
            AND {capture_predicate}
            AND (org_id=:org OR (org_id IS NULL AND CAST(:org AS INTEGER) IS NULL))
            AND (model_id=:model OR (model_id IS NULL AND CAST(:model AS INTEGER) IS NULL))
            AND (stored_path=:path OR (stored_path IS NULL AND CAST(:path AS TEXT) IS NULL))
            AND (CAST(:model AS INTEGER) IS NULL OR EXISTS (SELECT 1 FROM models m WHERE m.id=print_files.model_id
                 AND (m.org_id=:parent_org OR (m.org_id IS NULL AND CAST(:parent_org AS INTEGER) IS NULL))))
            AND (CAST(:printer AS INTEGER) IS NULL OR EXISTS (SELECT 1 FROM printers p WHERE p.id=:printer
                 AND ((p.org_id=:printer_org OR (p.org_id IS NULL AND CAST(:printer_org AS INTEGER) IS NULL))
                 AND (p.shared=:shared OR (p.shared IS NULL AND CAST(:shared AS BOOLEAN) IS NULL)))))
            AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.print_file_id=print_files.id OR s.model_id=print_files.model_id)
            AND NOT EXISTS (SELECT 1 FROM education_submissions s JOIN models m ON s.print_file_id=m.print_file_id WHERE m.id=print_files.model_id)
            AND NOT EXISTS (SELECT 1 FROM education_upload_operations o WHERE o.final_path=print_files.stored_path AND o.final_path IS NOT NULL)
            AND NOT EXISTS (SELECT 1 FROM print_files alias JOIN models m ON m.id=print_files.model_id
                 JOIN education_upload_operations o ON o.final_path=alias.stored_path AND o.final_path IS NOT NULL
                 WHERE alias.model_id=m.id OR alias.id=m.print_file_id)'''),
            {**capture_parameters, 'job': job_id, 'id': row.id, 'org': row.org_id, 'model': row.model_id, 'path': row.stored_path,
             'parent_org': parent.org_id if parent else None, 'printer': printer_id,
             'printer_org': target.org_id if target else None, 'shared': target.shared if target else None})
        if linked.rowcount != 1:
            raise HTTPException(409, 'Print file changed; refresh before scheduling')
        log_audit(db, 'print_file.scheduled', 'print_file', file_id, {'job_id': job_id})
        db.flush()
        db.commit()
        return {'job_id': job_id, 'file_id': file_id, 'project_name': row.project_name, 'status': 'pending'}
    except (HTTPException, OdinError):
        _rollback_file_transaction(db, file_id)
        raise
    except Exception as exc:
        _rollback_file_transaction(db, file_id)
        log.warning('Print file scheduling failed for file %s (%s)', file_id, type(exc).__name__)
        raise HTTPException(500, 'Unable to schedule print file. Refresh before retrying.') from None
