"""Reversible corrections of finished physical prints. No terminal event delivery.

The existing provenance store holds an exact before/after image. Stock-backed
quantity changes are refused: the filament ledger has no reversible refund.
"""
import json
import math
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import bindparam, text

from core.audit import log_audit
from core.controlled_parts import allowed
from core.filament_ledger import prior_attempt_usage
from core.interfaces.education_policy import is_education_reserved_filename
from core.order_production import prepare_print_job_write
from core.print_attempts import sqlalchemy_runner
from core.rbac import check_org_access
from modules.archives.services import assert_archive_access
from modules.jobs.education_guard import assert_generic_job_mutation_allowed
from modules.jobs.state_machine import transition_job

PREFIX = 'quota_attempt:print_failure:'
REASONS = {
    'no_filament': 'No filament fed / printed air',
    'filament_supply': 'Filament ran out or tangled',
    'first_layer': 'First layer did not stick',
    'came_loose': 'Print came loose or spaghetti',
    'under_extrusion': 'Nozzle clog or under-extrusion',
    'printer_fault': 'Printer fault or error code',
    'power_connection': 'Power or connection loss',
    'other': 'Other',
}
ARCHIVE_FIELDS = ('status', 'filament_used_grams', 'consumption_json')
PRINT_FIELDS = ('status', 'filament_used_g')
JOB_FIELDS = ('status', 'queue_position', 'updated_at', 'fail_reason', 'fail_notes')


def fail(message):
    raise HTTPException(409, message)


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def record(db, identifier):
    value = db.execute(text('SELECT value FROM system_config WHERE key=:key'),
                       {'key': PREFIX + str(identifier)}).scalar()
    saved = decoded(value) if value else None
    return saved if saved and _same_print(db, identifier, saved) else None


def _same_print(db, identifier, saved):
    row = db.execute(text('SELECT economic_birth_nonce,started_at,ended_at,printer_id,scheduled_job_id FROM print_jobs WHERE id=:id'), {'id': identifier}).mappings().first()
    return matches_print(row, saved)


def matches_print(row, saved):
    return bool(row and row['economic_birth_nonce'] == saved['birth']
                and str(row['started_at']) == saved['started_at'] and str(row['ended_at']) == saved['ended_at']
                and row['printer_id'] == saved['printer_id'] and row['scheduled_job_id'] == saved['job_id'])


def annotate(db, rows, *, archive=False, user=None):
    """Only call after the caller has applied the source record's audience rules."""
    ids = {row.get('print_job_id') if archive else row['id'] for row in rows}
    keys = [PREFIX + str(identifier) for identifier in ids if identifier is not None]
    values = {}
    if keys:
        query = text('SELECT key,value FROM system_config WHERE key IN :keys').bindparams(bindparam('keys', expanding=True))
        for key, value in db.execute(query, {'keys': keys}):
            saved = decoded(value)
            if saved.get('active') and _same_print(db, int(key.removeprefix(PREFIX)), saved):
                if user is not None:
                    try:
                        _load(db, int(key.removeprefix(PREFIX)), user)
                    except HTTPException:
                        continue
                values[key] = {k: saved[k] for k in ('reason', 'reason_label', 'note', 'by', 'at', 'original_grams', 'corrected_grams')}
    return [dict(row, failure_correction=values.get(PREFIX + str(row.get('print_job_id') if archive else row['id']))) for row in rows]


def _load(db, identifier, user):
    row = db.execute(text('SELECT p.*,r.org_id AS printer_org FROM print_jobs p JOIN printers r ON r.id=p.printer_id WHERE p.id=:id'),
                     {'id': identifier}).mappings().first()
    if not row or not check_org_access(user, row['printer_org']) or not allowed(db, 'print_job', identifier, user):
        raise HTTPException(404, 'Print not found')
    job = None
    if row['scheduled_job_id'] is not None:
        job = db.execute(text('SELECT * FROM jobs WHERE id=:id'), {'id': row['scheduled_job_id']}).mappings().first()
        if not job or not check_org_access(user, job['charged_to_org_id']):
            raise HTTPException(404, 'Print not found')
        assert_generic_job_mutation_allowed(db, job['id'])
    if any(is_education_reserved_filename(row[k]) for k in ('filename', 'job_name')):
        fail('This print must be managed through its Education workflow.')
    archives = db.execute(text('SELECT * FROM print_archives WHERE print_job_id=:id ORDER BY id'), {'id': identifier}).mappings().all()
    for archive in archives:
        assert_archive_access(db, archive['id'], user)
    from core.interfaces.education_policy import generic_archive_audience_sql
    # Includes archived Education content whose original job was deleted.
    education = generic_archive_audience_sql()
    if db.execute(text('SELECT 1 FROM print_archives a WHERE a.print_job_id=:id AND NOT (' + education + ')'), {'id': identifier}).first():
        fail('This print must be managed through its Education workflow.')
    return row, job, archives


def _settled(db, row, job, archives):
    if not row['ended_at'] or row['status'] not in ('cancelled', 'failed'):
        fail('Only a finished, cancelled print can be marked as failed.')
    if not archives:
        fail('This print has no archive yet. Try again after its archive is recorded.')
    if any(a['status'] != row['status'] for a in archives):
        fail('The print and its archive disagree. Resolve their history before making a correction.')
    question = db.execute(text('SELECT value FROM system_config WHERE key=:key'),
                          {'key': 'restart_question:' + str(row['printer_id'])}).scalar()
    if question:
        question = decoded(question)
        if question.get('state') in ('pending', 'investigation'):
            fail('Resolve the open print confirmation on this printer first.')
    if db.execute(text("SELECT 1 FROM printer_dispatch_claims WHERE printer_id=:printer AND state IN ('preparing','adapter_entered','accepted','uncertain')"),
                  {'printer': row['printer_id']}).first():
        fail('Resolve the pending print start on this printer first.')
    if db.execute(text("SELECT 1 FROM dispatch_sender_registrations WHERE job_id=:job AND state<>'quiesced'"), {'job': row['scheduled_job_id']}).first():
        fail('Resolve the pending print start for this job first.')
    if job and job['status'] not in ('cancelled', 'failed', 'completed'):
        fail('Finish or resolve the linked job before correcting this print.')
    # These immutable observations also retain an outcome. Never contradict them.
    if row['economic_uid'] and db.execute(text('SELECT 1 FROM terminal_filament_measurements WHERE attempt_uid=:uid'), {'uid': row['economic_uid']}).first():
        fail('This print has a retained material cost record that cannot be changed exactly.')


def _quantity_allowed(db, row, archives):
    try:
        _quantity_proof(db, row, archives)
    except (ValueError, TypeError, KeyError, AttributeError):
        fail('The original material record is incomplete and cannot be restored exactly.')


def _quantity_proof(db, row, archives):
    from core.interfaces.filament_ownership import uses_external_filament_accounting
    if uses_external_filament_accounting(row['printer_id']):
        fail('Material for this printer is managed outside ODIN and cannot be corrected here.')
    if prior_attempt_usage(sqlalchemy_runner(db), row['scheduled_job_id'], row['id']) or db.execute(
            text('SELECT 1 FROM spool_usage WHERE print_job_id=:id OR (job_id=:job AND :job IS NOT NULL)'),
            {'id': row['id'], 'job': row['scheduled_job_id']}).first():
        fail('Material was already deducted from inventory. ODIN cannot return and restore that deduction exactly.')
    if db.execute(text('SELECT 1 FROM filament_delivery_outbox WHERE print_job_id=:id'), {'id': row['id']}).first():
        fail('Material was sent to an inventory service and cannot be restored exactly.')
    # Refuse pending/reconciled and stock-associated shapes, including AMS and
    # measured balances. A later tray confirmation must not invalidate undo.
    for archive in archives:
        if archive['accounting_state'] in ('accounted', 'reconciliation_required', 'externally_managed') or archive['accounting_reason'] == 'spool_change_pending':
            fail('This print has inventory accounting that cannot be reversed exactly.')
        entries = decoded(archive['consumption_json']) or []
        if not isinstance(entries, list) or any(not isinstance(e, dict) or e.get('spool_id') is not None or e.get('identity_state') for e in entries):
            fail('The original spool use cannot be restored exactly.')
        if len(entries) > 1:
            fail('Material use for several filaments cannot be divided exactly for this print.')
    snapshot = decoded(row['spool_snapshot']) if row['spool_snapshot'] is not None else {}
    if not isinstance(snapshot, dict):
        fail('The original material record is incomplete and cannot be restored exactly.')
    plan = snapshot.get('material_plan')
    if plan is None:
        plan = {}
    if not isinstance(plan, dict):
        fail('The original material record is incomplete and cannot be restored exactly.')
    if len(plan.get('allocations') or []) > 1:
        fail('Material use for several filaments cannot be divided exactly for this print.')
    if any(item.get('spool_id') is not None or item.get('identity_state') for item in
           [*(snapshot.get('slots') or []), *(snapshot.get('located_spools') or []), *(plan.get('allocations') or [])]):
        fail('This print refers to a spool whose original balance cannot be restored exactly.')


def _image(row, fields):
    return {key: row[key].isoformat(sep=' ') if isinstance(row[key], datetime) else row[key] for key in fields}


def _write_print(db, identifier, values):
    db.execute(text('UPDATE print_jobs SET status=:status,filament_used_g=:filament_used_g WHERE id=:id'), dict(values, id=identifier))


def _write_archive(db, identifier, values):
    db.execute(text('UPDATE print_archives SET status=:status,filament_used_grams=:filament_used_grams,consumption_json=:consumption_json WHERE id=:id'), dict(values, id=identifier))


def change(db, identifier, user, body=None):
    """Caller commits/rolls back the correction, provenance and audit together."""
    if user.get('role') not in ('operator', 'admin'):
        raise HTTPException(403, 'Only operators and administrators can correct a print.')
    row, _, _ = _load(db, identifier, user)
    # Serialize with absent restart questions and legacy terminal writers too.
    # SQLite's first write takes the database writer lock. PG refuses contention
    # instead of waiting while holding part of a multi-parent lock graph.
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('LOCK TABLE printers, jobs, print_jobs, print_archives, printer_dispatch_claims, dispatch_sender_registrations, system_config IN SHARE ROW EXCLUSIVE MODE NOWAIT'))
    db.execute(text('UPDATE printers SET id=id WHERE id=:id'), {'id': row['printer_id']})
    prepare_print_job_write(db, identifier)
    row, job, archives = _load(db, identifier, user)
    _settled(db, row, job, archives)
    saved = record(db, identifier)
    if body is None:
        return _undo(db, row, job, archives, saved, user)
    if saved and saved.get('active'):
        fail('This print is already marked as failed. Use Back to cancelled before making another correction.')
    if row['status'] != 'cancelled':
        fail('Only a finished, cancelled print can be marked as failed.')
    original = row['filament_used_g']
    figures = [value for value in [original, *(a['filament_used_grams'] for a in archives)] if value is not None]
    corrected = body.filament_used_g
    if body.no_material_used:
        if corrected not in (None, 0):
            raise HTTPException(422, 'No material was used requires zero grams.')
        corrected = 0.0
    quantity_changed = corrected is not None and (original != corrected or any(a['filament_used_grams'] != corrected for a in archives))
    if corrected is not None:
        if not figures or any(not math.isfinite(value) or value < corrected for value in figures):
            fail('Corrected material use cannot exceed the recorded amount or replace an unknown amount.')
        if quantity_changed:
            _quantity_allowed(db, row, archives)
    before = {'print': _image(row, PRINT_FIELDS), 'archives': {str(a['id']): _image(a, ARCHIVE_FIELDS) for a in archives}, 'job': None}
    after = {'print': dict(before['print'], status='failed'), 'archives': {k: dict(v, status='failed') for k, v in before['archives'].items()}, 'job': None}
    if quantity_changed:
        after['print']['filament_used_g'] = corrected
        for values in after['archives'].values():
            values['filament_used_grams'] = corrected
            entries = decoded(values['consumption_json']) or []
            if entries:
                entries[0]['grams_used'] = corrected
                entries[0]['quantity_provenance'] = 'operator_corrected'
                values['consumption_json'] = json.dumps(entries)
    # A historical attempt must never rewrite the outcome of a later retry.
    latest = db.execute(text('SELECT MAX(id) FROM print_jobs WHERE scheduled_job_id=:job'), {'job': row['scheduled_job_id']}).scalar()
    if job and latest == identifier and job['status'] == 'cancelled':
        before['job'] = _image(job, JOB_FIELDS)
        after['job'] = dict(before['job'], status='failed', fail_reason=REASONS[body.reason], fail_notes=body.note or None)
        _write_job(db, job['id'], after['job'])
    _write_print(db, identifier, after['print'])
    for aid, values in after['archives'].items():
        _write_archive(db, int(aid), values)
    saved = dict(active=True, reason=body.reason, reason_label=REASONS[body.reason], note=body.note,
                 by=user.get('username') or 'Operator', user_id=user.get('id'), at=datetime.now(timezone.utc).isoformat(),
                 original_grams=original, corrected_grams=after['print']['filament_used_g'], before=before, after=after,
                 quantity_changed=quantity_changed, job_id=row['scheduled_job_id'], printer_id=row['printer_id'],
                 birth=row['economic_birth_nonce'], started_at=str(row['started_at']), ended_at=str(row['ended_at']))
    _save(db, identifier, saved)
    log_audit(db, 'print.marked_failed', 'print_job', identifier, saved)
    return {'id': identifier, 'status': 'failed'}


def _write_job(db, identifier, values):
    if not transition_job(db, identifier, values['status'], 'outcome_correction',
                          extra_fields={key: values[key] for key in JOB_FIELDS if key != 'status'}):
        fail('The linked job changed. Reload before correcting this print.')


def _save(db, identifier, saved):
    db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value) ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
               {'key': PREFIX + str(identifier), 'value': json.dumps(saved, allow_nan=False)})


def _undo(db, row, job, archives, saved, user):
    if not saved or not saved.get('active'):
        fail('This print has no failure correction to undo.')
    if (row['scheduled_job_id'] != saved['job_id'] or row['printer_id'] != saved['printer_id'] or
            row['economic_birth_nonce'] != saved['birth'] or str(row['started_at']) != saved['started_at'] or str(row['ended_at']) != saved['ended_at'] or
            _image(row, PRINT_FIELDS) != saved['after']['print'] or
            {str(a['id']): _image(a, ARCHIVE_FIELDS) for a in archives} != saved['after']['archives']):
        fail('The print record changed after the correction. It cannot be restored exactly.')
    if saved['quantity_changed']:
        _quantity_allowed(db, row, archives)
    if saved['before']['job'] is not None:
        latest = db.execute(text('SELECT MAX(id) FROM print_jobs WHERE scheduled_job_id=:job'), {'job': saved['job_id']}).scalar()
        if not job or latest != row['id'] or _image(job, JOB_FIELDS) != saved['after']['job']:
            fail('The linked job changed after the correction. It cannot be restored exactly.')
        _write_job(db, job['id'], saved['before']['job'])
    _write_print(db, row['id'], saved['before']['print'])
    for aid, values in saved['before']['archives'].items():
        _write_archive(db, int(aid), values)
    saved.update(active=False, undone_by=user.get('username') or 'Operator', undone_by_id=user.get('id'), undone_at=datetime.now(timezone.utc).isoformat())
    _save(db, row['id'], saved)
    log_audit(db, 'print.failure_undone', 'print_job', row['id'], saved)
    return {'id': row['id'], 'status': 'cancelled'}
