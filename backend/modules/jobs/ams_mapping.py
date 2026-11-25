"""Generic job AMS edits, serialized with dispatch claim acquisition."""
import hashlib
import os
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from core.ams_mapping import validate_for_printer, preview
from core.ams_mapping import mapping_audit_details
from core.print_payload import MappingMaterialMismatch
from core.rbac import check_org_access
from modules.jobs.education_guard import assert_generic_job_mutation_allowed
from modules.jobs.pinning import job_pin, pin_columns, pinned_printer_id


def lock_mapping_edit(db, job_id, updates):
    # Target edits also serialize: a retained mapping may never drift to another printer.
    if not updates.model_fields_set.intersection({'ams_mapping', 'printer_id', 'pinned_printer_id', 'model_id', 'hold'}):
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
        raise HTTPException(409, "Job has an active dispatch claim. Filament mapping cannot change. An administrator can release it from the printer's Dispatch recovery panel.")
    # A dispatched attempt can outlive its claim; never edit after start was recorded.
    if job.actual_start is not None:
        raise HTTPException(409, 'AMS mapping cannot change after dispatch')


def checked_changes(db, actor, job, changes):
    revision = changes.pop('ams_file_revision', None)
    explicit = 'ams_mapping' in changes
    target = changes.get('printer_id', job.printer_id)
    pin = pinned_printer_id(changes.get('target_type', job.target_type), changes.get('target_filter', job.target_filter))
    if not explicit:
        if job.ams_mapping is not None and (target != job.printer_id or pin != job_pin(job)):
            raise HTTPException(409, 'Clear or replace the AMS mapping when changing its target printer')
        if job.hold and changes.get('hold') is False:
            assert_release_ready(db, actor, job)
        return changes
    assert_editable(db, job)
    mapping = changes['ams_mapping']
    if mapping is None and job.hold and changes.get('hold') is False:
        assert_release_ready(db, actor, job)
    if mapping is not None:
        if pin is not None and target is not None and pin != target:
            raise HTTPException(409, 'AMS mapping target must match the assigned printer')
        target = pin or target
        if job.print_file_id is None and revision:
            file = bind_reviewed_rack_file(db, job, actor, changes, target, revision)
        else:
            file = source_file(db, job, actor)
            if revision and file_revision(file, target) != revision:
                raise HTTPException(409, 'The file has changed. Review the mapping again.')
        changes['ams_mapping'] = validate_for_printer(
            db, vector_entries(mapping, file.filaments_json), file.filaments_json, target, actor,
            stored_path=file.stored_path, check_material=changes.get('hold', job.hold) is not True)
        changes.update(pin_columns(target))
        changes['printer_id'] = target
    return changes


def vector_entries(mapping, filaments_json):
    """Read the stored protocol vector GET serves as the entries a dict write sends.

    The vector is indexed by file ID minus one: every file ID holds a zero-based
    tray, every other position holds -1. Anything else is refused, never guessed;
    tray bounds and materials are then checked exactly as for the dict form.
    """
    from core.print_payload import source_filaments
    if not mapping or not all(type(slot) is int for slot in mapping):
        return mapping
    try:
        ids = {f['slot'] for f in source_filaments(filaments_json)}
    except HTTPException:
        # No usable filaments: validate_mapping refuses it exactly as for entries.
        return mapping
    if not ids:
        return mapping
    if len(mapping) != max(ids):
        raise HTTPException(400, 'AMS mapping must cover every source filament')
    if any((slot < 0) if index + 1 in ids else (slot != -1) for index, slot in enumerate(mapping)):
        raise HTTPException(400, 'Invalid or duplicate AMS slot')
    return [{'source_slot': index + 1, 'target_slot': slot + 1}
            for index, slot in enumerate(mapping) if index + 1 in ids]


def source_file(db, job, actor, *, file_id=None):
    from core.model_preview_access import accessible_preview_file
    try:
        file = accessible_preview_file(db, file_id if file_id is not None else job.print_file_id, actor)
    except HTTPException:
        raise HTTPException(400, 'AMS mapping requires an accessible job print file') from None
    if file.model_id != job.model_id:
        raise HTTPException(400, 'AMS mapping requires the job source print file')
    if file.model_id is not None:
        model = db.execute(text('SELECT org_id FROM models WHERE id=:id'), {'id': file.model_id}).first()
        if not model or not check_org_access(actor, model.org_id):
            raise HTTPException(400, 'AMS mapping requires an accessible job print file')
    return file


def file_revision(file, printer_id):
    """An opaque preview identity for a model-selected file, before its first explicit binding."""
    try:
        stat = os.stat(file.stored_path, follow_symlinks=False)
    except (OSError, TypeError):
        return None
    identity = (file.id, printer_id, stat.st_dev, stat.st_ino, stat.st_size, stat.st_ctime_ns)
    return hashlib.sha256(repr(identity).encode()).hexdigest()


def bind_reviewed_rack_file(db, job, actor, changes, target, revision):
    """Bind the reviewed slice on the first explicit mapping of a model-selected job."""
    from core.ams_mapping import accessible_printer
    from core.print_file_binding import job_file_id, require_bound_target
    from core.model_preview_access import is_education_owned_file
    from core.print_payload_snapshot import hash_regular_payload, PayloadUnavailable
    from modules.printers.services import is_h3c_printer
    from modules.jobs.education_guard import assert_generic_model_reference_allowed
    printer = accessible_printer(db, target, actor)
    if changes.get('model_id', job.model_id) != job.model_id or not is_h3c_printer(printer._mapping):
        raise HTTPException(409, 'Review the mapping for the selected file and printer.')
    file = source_file(db, job, actor, file_id=job_file_id(db, job.model_id))
    if file.blob_state != 'present' or file_revision(file, target) != revision:
        raise HTTPException(409, 'The file has changed. Review the mapping again.')
    assert_generic_model_reference_allowed(db, file.model_id)
    if is_education_owned_file(db, file.id, file.stored_path):
        raise HTTPException(409, 'Education files require their approval workflow.')
    require_bound_target(db, file.id, target)
    try:
        digest = hash_regular_payload(file.stored_path)
    except (OSError, PayloadUnavailable):
        raise HTTPException(409, 'The print file could not be verified.') from None
    if (any(value and value.lower() != digest for value in (file.file_hash, job.payload_sha256))
            or file_revision(file, target) != revision):
        raise HTTPException(409, 'The file has changed. Review the mapping again.')
    changes.update(print_file_id=file.id, plate_index=job.plate_index or 0, payload_sha256=digest)
    return file


def job_preview(db, job, actor, target):
    from core.ams_mapping import accessible_printer
    from core.print_file_binding import job_file_id
    from modules.printers.services import is_h3c_printer
    try:
        if job.print_file_id is None and not is_h3c_printer(accessible_printer(db, target, actor)._mapping):
            return None
        file_id = job_file_id(db, job.model_id, job.print_file_id)
        file = source_file(db, job, actor, file_id=file_id)
        revision = file_revision(file, target) if job.print_file_id is None else None
        result = preview(db, file.filaments_json, target, actor, stored_path=file.stored_path,
                         plate_index=job.plate_index or 0, stored_mapping=job.ams_mapping)
        if revision and file.blob_state == 'present' and result and any('eligible' in row for row in result['required']):
            from core.model_preview_access import is_education_owned_file
            if not is_education_owned_file(db, file.id, file.stored_path):
                result['ams_file_revision'] = revision
        return result
    except HTTPException:
        return None  # An unavailable or inaccessible file must not hide the job itself.


def detail_view(db, job, actor):
    from modules.jobs.schemas import JobResponse
    from core.display_names import name_jobs
    from modules.jobs.display_facts import enrich_job_facts
    enrich_job_facts(db, name_jobs(db, [job], actor), actor)
    result = JobResponse.model_validate(job).model_dump()
    result.update(ams_preview=None, ams_mapping_editable=False)
    if job.print_file_id is None and job.model_id is None:
        return result
    try:
        assert_editable(db, job)
    except HTTPException:
        return result
    target = job_pin(job) or job.printer_id
    if target is None:
        return result
    result['ams_preview'] = job_preview(db, job, actor, target)
    if job.hold and result['ams_preview'] is not None:
        result['ams_preview']['planned_loads'] = planned_loads(job, result['ams_preview'])
    facts = result['ams_preview']
    result['ams_mapping_editable'] = facts is not None and (job.print_file_id is not None or bool(facts.get('ams_file_revision')))
    return result


def assert_release_ready(db, actor, job):
    """Validate the saved protocol vector against current trays before releasing."""
    if not job.hold or job.ams_mapping is None:
        return
    file = source_file(db, job, actor)
    mapping = [{'source_slot': i + 1, 'target_slot': slot + 1}
               for i, slot in enumerate(job.ams_mapping) if slot >= 0]
    validate_for_printer(db, mapping, file.filaments_json, job_pin(job) or job.printer_id, actor, stored_path=file.stored_path)


def checked_repeat_mapping(db, actor, original):
    """Return a validated mapping and whether the repeat needs a planned hold."""
    if original.ams_mapping is None:
        return None, False
    file = source_file(db, original, actor)
    mapping = [{'source_slot': i + 1, 'target_slot': slot + 1}
               for i, slot in enumerate(original.ams_mapping) if slot >= 0]
    args = (db, mapping, file.filaments_json, job_pin(original) or original.printer_id, actor)
    # Check every slot first: an early material mismatch must not hide a later structural error.
    validated = validate_for_printer(*args, stored_path=file.stored_path, check_material=False)
    if original.hold:
        return validated, True
    try:
        validate_for_printer(*args, stored_path=file.stored_path)
    except MappingMaterialMismatch:
        return validated, True
    return validated, False


def planned_loads(job, facts):
    """Read-only live load instructions; never persist a readiness flag."""
    from core.material_identity import canonical_reported_material
    if not job.hold or job.ams_mapping is None:
        return []
    available = {slot['slot']: slot['material'] for slot in facts['available']}
    loads = []
    for source in facts['required']:
        index = source['slot'] - 1
        target = job.ams_mapping[index] + 1 if index < len(job.ams_mapping) else 0
        material = canonical_reported_material(source['material'])
        if target < 1 or target not in available:
            loads.append(f"remap filament {source['slot']} to an available tray")
        elif material is None:
            loads.append(f"verify source material for filament {source['slot']}")
        elif material != canonical_reported_material(available[target]):
            label = f"AMS HT {target - 16}" if 17 <= target <= 24 else f"AMS {(target - 1) // 4 + 1} slot {(target - 1) % 4 + 1}"
            loads.append(f"load {material} in {label}")
    return list(dict.fromkeys(loads))
