"""Generic job AMS edits, serialized with dispatch claim acquisition."""
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
        file = source_file(db, job, actor)
        changes['ams_mapping'] = validate_for_printer(
            db, vector_entries(mapping, file.filaments_json), file.filaments_json, target, actor,
            check_material=changes.get('hold', job.hold) is not True)
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
    ids = {f['slot'] for f in source_filaments(filaments_json)}
    if len(mapping) != max(ids):
        raise HTTPException(400, 'AMS mapping must cover every source filament')
    if any((slot < 0) if index + 1 in ids else (slot != -1) for index, slot in enumerate(mapping)):
        raise HTTPException(400, 'Invalid or duplicate AMS slot')
    return [{'source_slot': index + 1, 'target_slot': slot + 1}
            for index, slot in enumerate(mapping) if index + 1 in ids]


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
    from core.display_names import name_jobs
    from modules.jobs.display_facts import enrich_job_facts
    enrich_job_facts(db, name_jobs(db, [job], actor), actor)
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
    if job.hold and result['ams_preview'] is not None:
        result['ams_preview']['planned_loads'] = planned_loads(job, result['ams_preview'])
    result['ams_mapping_editable'] = result['ams_preview'] is not None
    return result


def assert_release_ready(db, actor, job):
    """Validate the saved protocol vector against current trays before releasing."""
    if not job.hold or job.ams_mapping is None:
        return
    file = source_file(db, job, actor)
    mapping = [{'source_slot': i + 1, 'target_slot': slot + 1}
               for i, slot in enumerate(job.ams_mapping) if slot >= 0]
    validate_for_printer(db, mapping, file.filaments_json, job_pin(job) or job.printer_id, actor)


def checked_repeat_mapping(db, actor, original):
    """Return a validated mapping and whether the repeat needs a planned hold."""
    if original.ams_mapping is None:
        return None, False
    file = source_file(db, original, actor)
    mapping = [{'source_slot': i + 1, 'target_slot': slot + 1}
               for i, slot in enumerate(original.ams_mapping) if slot >= 0]
    args = (db, mapping, file.filaments_json, job_pin(original) or original.printer_id, actor)
    # Check every slot first: an early material mismatch must not hide a later structural error.
    validated = validate_for_printer(*args, check_material=False)
    if original.hold:
        return validated, True
    try:
        validate_for_printer(*args)
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
            loads.append(f"load {material} in AMS {(target - 1) // 4 + 1} slot {(target - 1) % 4 + 1}")
    return list(dict.fromkeys(loads))
