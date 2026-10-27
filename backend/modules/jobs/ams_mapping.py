"""Generic job AMS edits, serialized with dispatch claim acquisition."""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from core.ams_mapping import validate_for_printer, preview
from core.ams_mapping import mapping_audit_details
from core.rbac import check_org_access
from modules.jobs.education_guard import assert_generic_job_mutation_allowed
from modules.jobs.pinning import job_pin, pin_columns, pinned_printer_id


def lock_mapping_edit(db, job_id, updates):
    # Target edits also serialize: a retained mapping may never drift to another printer.
    if not updates.model_fields_set.intersection({'ams_mapping', 'printer_id', 'pinned_printer_id', 'model_id'}):
        return
    statement = ('SELECT id FROM jobs WHERE id=:id FOR UPDATE NOWAIT'
                 if db.get_bind().dialect.name == 'postgresql' else
                 'UPDATE jobs SET id=id WHERE id=:id')
    try:
        db.execute(text(statement), {'id': job_id})
    except OperationalError:
        db.rollback()
        raise HTTPException(409, 'Job is busy; reload before editing its AMS mapping') from None


def assert_editable(db, job):
    assert_generic_job_mutation_allowed(db, job.id)
    if job.status not in ('pending', 'scheduled'):
        raise HTTPException(409, 'AMS mapping can only change on a pending, held or scheduled job')
    if db.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE job_id=:id'), {'id': job.id}).first():
        raise HTTPException(409, 'Job has an active dispatch claim; AMS mapping cannot change')
    # A dispatched attempt can outlive its claim; never edit after start was recorded.
    if job.actual_start is not None:
        raise HTTPException(409, 'AMS mapping cannot change after dispatch')


def checked_changes(db, actor, job, changes):
    explicit = 'ams_mapping' in changes
    target = changes.get('printer_id', job.printer_id)
    pin = pinned_printer_id(changes.get('target_type', job.target_type), changes.get('target_filter', job.target_filter))
    if not explicit:
        if job.ams_mapping is not None and (target != job.printer_id or pin != job_pin(job)):
            raise HTTPException(409, 'Clear or replace the AMS mapping when changing its target printer')
        return changes
    assert_editable(db, job)
    mapping = changes['ams_mapping']
    if mapping is not None:
        if pin is not None and target is not None and pin != target:
            raise HTTPException(409, 'AMS mapping target must match the assigned printer')
        target = pin or target
        file = source_file(db, job, actor)
        changes['ams_mapping'] = validate_for_printer(db, mapping, file.filaments_json, target, actor)
        changes.update(pin_columns(target))
        changes['printer_id'] = target
    return changes


def source_file(db, job, actor):
    from core.model_preview_access import accessible_preview_file
    try:
        file = accessible_preview_file(db, job.print_file_id, actor)
    except HTTPException:
        raise HTTPException(400, 'AMS mapping requires an accessible job print file') from None
    if file.model_id != job.model_id:
        raise HTTPException(400, 'AMS mapping requires the job source print file')
    if file.model_id is not None:
        model = db.execute(text('SELECT org_id FROM models WHERE id=:id'), {'id': file.model_id}).first()
        if not model or not check_org_access(actor, model.org_id):
            raise HTTPException(400, 'AMS mapping requires an accessible job print file')
    return file


def detail_view(db, job, actor):
    from modules.jobs.schemas import JobResponse
    result = JobResponse.model_validate(job).model_dump()
    result.update(ams_preview=None, ams_mapping_editable=False)
    if job.print_file_id is None:
        return result
    try:
        assert_editable(db, job)
    except HTTPException:
        return result
    target = job_pin(job) or job.printer_id
    if target is None:
        return result
    try:
        file = source_file(db, job, actor)
        result['ams_preview'] = preview(db, file.filaments_json, target, actor)
    except HTTPException:
        # An optional suggestion must never hide an otherwise accessible job.
        return result
    result['ams_mapping_editable'] = result['ams_preview'] is not None
    return result
