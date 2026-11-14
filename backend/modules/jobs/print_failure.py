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
from core.order_production import ProductionLinkageChanged, prepare_job_write, prepare_print_job_write
from core.print_attempts import attempt_seq, sqlalchemy_runner
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
ARCHIVE_FIELDS = ('status', 'filament_used_grams', 'consumption_json', 'cost_estimate')
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
                and row['printer_id'] == saved['printer_id'])


def annotate(db, rows, *, archive=False, user=None):
    """Only call after the caller has applied the source record's audience rules."""
    ids = sorted({row.get('print_job_id') if archive else row['id'] for row in rows} - {None})
    values = {}
    authorized = set()
    if ids:
        from core.controlled_history import media_visibility
        from modules.archives.services import archive_visibility
        controlled, parameters = media_visibility(user, 'p.scheduled_job_id')
        audience, bindings = archive_visibility(user)
        parameters.update(bindings)
        # One bounded batch for provenance, immutable print identity and read
        # audience. Job mutation eligibility is deliberately not a read policy.
        query = text('SELECT p.*,r.org_id AS printer_org,j.charged_to_org_id AS job_org,c.value '
                     'FROM print_jobs p JOIN printers r ON r.id=p.printer_id '
                     'LEFT JOIN jobs j ON j.id=p.scheduled_job_id '
                     'LEFT JOIN system_config c ON c.key=:prefix || CAST(p.id AS TEXT) '
                     'WHERE p.id IN :ids AND (' + controlled + ') AND NOT EXISTS '
                     '(SELECT 1 FROM print_archives a WHERE a.print_job_id=p.id AND NOT COALESCE((' + audience + '), FALSE))'
                     ).bindparams(bindparam('ids', expanding=True))
        for start in range(0, len(ids), 500):
            for row in db.execute(query, dict(parameters, prefix=PREFIX, ids=ids[start:start + 500])).mappings():
                if not check_org_access(user, row['printer_org']) or not check_org_access(user, row['job_org']):
                    continue
                if user and user.get('role') in ('operator', 'admin'):
                    authorized.add(row['id'])
                saved = decoded(row['value']) if row['value'] else None
                if saved and saved.get('active') and matches_print(row, saved):
                    values[row['id']] = {k: saved[k] for k in ('reason', 'reason_label', 'note', 'by', 'at', 'original_grams', 'corrected_grams')}
    return [dict(row, failure_correction=values.get(row.get('print_job_id') if archive else row['id']),
                 failure_correction_allowed=(row.get('print_job_id') if archive else row['id']) in authorized) for row in rows]


def _load(db, identifier, user, *, undo=False):
    row = db.execute(text('SELECT p.*,r.org_id AS printer_org FROM print_jobs p JOIN printers r ON r.id=p.printer_id WHERE p.id=:id'),
                     {'id': identifier}).mappings().first()
    if not row or not check_org_access(user, row['printer_org']) or not allowed(db, 'print_job', identifier, user):
        raise HTTPException(404, 'Print not found')
    job = None
    if row['scheduled_job_id'] is not None:
        job = db.execute(text('SELECT * FROM jobs WHERE id=:id'), {'id': row['scheduled_job_id']}).mappings().first()
        if job and not check_org_access(user, job['charged_to_org_id']):
            raise HTTPException(404, 'Print not found')
        if job and not undo:
            assert_generic_job_mutation_allowed(db, job['id'])
    if not undo and any(is_education_reserved_filename(row[k]) for k in ('filename', 'job_name')):
        fail('This print must be managed through its Education workflow.')
    archives = db.execute(text('SELECT * FROM print_archives WHERE print_job_id=:id ORDER BY id'), {'id': identifier}).mappings().all()
    for archive in archives:
        assert_archive_access(db, archive['id'], user)
    from core.interfaces.education_policy import generic_archive_audience_sql
    # Includes archived Education content whose original job was deleted.
    education = generic_archive_audience_sql()
    if not undo and db.execute(text('SELECT 1 FROM print_archives a WHERE a.print_job_id=:id AND NOT COALESCE((' + education + '), FALSE)'), {'id': identifier}).first():
        fail('This print must be managed through its Education workflow.')
    return row, job, archives


def _settled(db, row, job, archives, *, undo=False):
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
    if not undo and job and job['status'] not in ('cancelled', 'failed', 'completed'):
        fail('Finish or resolve the linked job before correcting this print.')
    # These immutable observations also retain an outcome. Never contradict them.
    if row['economic_uid'] and db.execute(text('SELECT 1 FROM terminal_filament_measurements WHERE attempt_uid=:uid'), {'uid': row['economic_uid']}).first():
        fail('This print has a retained material cost record that cannot be changed exactly.')


def _quantity_allowed(db, row, archives, corrected=None):
    try:
        _quantity_proof(db, row, archives, corrected)
    except (ValueError, TypeError, KeyError, AttributeError):
        fail('The original material record is incomplete and cannot be restored exactly.')


def _quantity_proof(db, row, archives, corrected):
    from core.interfaces.filament_ownership import uses_external_filament_accounting
    if row['economic_uid'] and db.execute(text('SELECT 1 FROM terminal_filament_measurements WHERE attempt_uid=:uid'), {'uid': row['economic_uid']}).first():
        fail('This print has a retained material cost record that cannot be changed exactly.')
    if uses_external_filament_accounting(row['printer_id']):
        fail('Material for this printer is managed outside ODIN and cannot be corrected here.')
    prior = prior_attempt_usage(sqlalchemy_runner(db), row['scheduled_job_id'], row['id'])
    if prior:
        own_usage = db.execute(text('SELECT 1 FROM spool_usage WHERE print_job_id=:id'), {'id': row['id']}).first()
        if not own_usage:
            fail('An existing attempt-level inventory deduction cannot be attributed exactly to this print. Correct only the outcome or resolve that accounting first.')
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
    snapshot = decoded(row['spool_snapshot']) if row['spool_snapshot'] is not None else {}
    if not isinstance(snapshot, dict):
        fail('The original material record is incomplete and cannot be restored exactly.')
    plan = snapshot.get('material_plan')
    if plan is None:
        plan = {}
    if not isinstance(plan, dict):
        fail('The original material record is incomplete and cannot be restored exactly.')
    if corrected != 0 and len(plan.get('allocations') or []) > 1 and not all(decoded(a['consumption_json']) for a in archives):
        fail('The per-filament recorded amounts are missing; the corrected total cannot be spread in proportion.')
    if any(item.get('spool_id') is not None or item.get('identity_state') for item in
           [*(snapshot.get('slots') or []), *(snapshot.get('located_spools') or []), *(plan.get('allocations') or [])]):
        fail('This print refers to a spool whose original balance cannot be restored exactly.')


def _image(row, fields):
    return {key: row[key].isoformat(sep=' ') if isinstance(row[key], datetime) else row[key] for key in fields}


def _write_print(db, identifier, values):
    db.execute(text('UPDATE print_jobs SET status=:status,filament_used_g=:filament_used_g WHERE id=:id'), dict(values, id=identifier))


def _write_archive(db, identifier, values):
    db.execute(text('UPDATE print_archives SET status=:status,filament_used_grams=:filament_used_grams,consumption_json=:consumption_json,cost_estimate=:cost_estimate WHERE id=:id'), dict(values, id=identifier))


def _recorded_grams(db, row, archives):
    from modules.jobs.cost_actual import load_recorded_usage
    usage = load_recorded_usage(db, [row['id']])[row['id']]
    if usage is not None:
        amounts = [item['grams'] for item in usage]
        return sum(amounts) if all(_amount(value) for value in amounts) and _amount(sum(amounts)) else None
    return next((a['filament_used_grams'] for a in archives if a['filament_used_grams'] is not None), row['filament_used_g'])


def _amount(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _spread(entries, corrected):
    if not entries:
        return entries
    if corrected == 0:
        return [dict(entry, grams_used=0.0, quantity_provenance='operator_corrected') for entry in entries]
    amounts = [e.get('grams_used') for e in entries]
    if not all(_amount(v) for v in amounts) or not math.isfinite(sum(amounts)) or (sum(amounts) == 0 and corrected != 0):
        fail('The per-filament amounts are missing or invalid; the corrected total cannot be spread in proportion.')
    total = sum(amounts)
    parts = [corrected * (v / total) if total else 0.0 for v in amounts]
    # Assign the floating-point remainder to the largest part, preserving the total.
    largest = amounts.index(max(amounts))
    parts[largest] = corrected - sum(v for i, v in enumerate(parts) if i != largest)
    if parts[largest] < 0:
        fail('The corrected total cannot be spread exactly across these filaments.')
    return [dict(entry, grams_used=part, quantity_provenance='operator_corrected') for entry, part in zip(entries, parts)]


def change(db, identifier, user, body=None):
    """Caller commits/rolls back the correction, provenance and audit together."""
    if user.get('role') not in ('operator', 'admin'):
        raise HTTPException(403, 'Only operators and administrators can correct a print.')
    row, _, _ = _load(db, identifier, user, undo=body is None)
    # Follow the existing parent -> job -> physical print write order. These
    # row writes serialize competing corrections without locking telemetry tables.
    if row['scheduled_job_id'] is not None:
        prepare_job_write(db, [row['scheduled_job_id']])
        db.execute(text('UPDATE jobs SET updated_at=updated_at WHERE id=:id'), {'id': row['scheduled_job_id']})
    try:
        linked_job, _ = prepare_print_job_write(db, identifier)
    except ProductionLinkageChanged:
        fail('The print linkage changed. Reload before correcting this print.')
    if linked_job != row['scheduled_job_id']:
        fail('The print linkage changed. Reload before correcting this print.')
    row, job, archives = _load(db, identifier, user, undo=body is None)
    saved = record(db, identifier)
    if body is None:
        return _undo(db, row, job, archives, saved, user)
    _settled(db, row, job, archives)
    if saved and saved.get('active'):
        fail('This print is already marked as failed. Use Back to cancelled before making another correction.')
    if row['status'] != 'cancelled':
        fail('Only a finished, cancelled print can be marked as failed.')
    original = _recorded_grams(db, row, archives)
    corrected = body.filament_used_g
    if body.no_material_used:
        if corrected not in (None, 0):
            raise HTTPException(422, 'No material was used requires zero grams.')
        corrected = 0.0
    quantity_changed = corrected is not None and (original != corrected or row['filament_used_g'] != corrected
        or any(a['filament_used_grams'] != corrected or (corrected == 0 and a['cost_estimate'] not in (None, 0)) for a in archives))
    if corrected is not None:
        if not _amount(original) or original < corrected:
            fail('Corrected material use cannot exceed the recorded amount or replace an unknown amount.')
        if quantity_changed:
            _quantity_allowed(db, row, archives, corrected)
    before = {'print': _image(row, PRINT_FIELDS), 'archives': {str(a['id']): _image(a, ARCHIVE_FIELDS) for a in archives}, 'job': None}
    after = {'print': dict(before['print'], status='failed'), 'archives': {k: dict(v, status='failed') for k, v in before['archives'].items()}, 'job': None}
    if quantity_changed:
        after['print']['filament_used_g'] = corrected
        for values in after['archives'].values():
            old_grams = values['filament_used_grams']
            old_cost = values['cost_estimate']
            values['cost_estimate'] = (0.0 if corrected == 0 else
                old_cost * corrected / old_grams if _amount(old_cost) and _amount(old_grams) and old_grams > 0 else None)
            values['filament_used_grams'] = corrected
            entries = decoded(values['consumption_json']) or []
            if not entries and corrected == 0:
                plan = (decoded(row['spool_snapshot']) or {}).get('material_plan') or {}
                entries = plan.get('allocations') or []
            if entries:
                values['consumption_json'] = json.dumps(_spread(entries, corrected))
    # A historical attempt must never rewrite the outcome of a later retry.
    latest = db.execute(text('SELECT MAX(id) FROM print_jobs WHERE scheduled_job_id=:job'), {'job': row['scheduled_job_id']}).scalar()
    if job and latest == identifier and job['status'] == 'cancelled':
        before['job'] = _image(job, JOB_FIELDS)
        after['job'] = dict(before['job'], status='failed', updated_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=' '), fail_reason=REASONS[body.reason], fail_notes=body.note or None)
        _write_job(db, job['id'], after['job'])
    _write_print(db, identifier, after['print'])
    for aid, values in after['archives'].items():
        _write_archive(db, int(aid), values)
    saved = dict(active=True, reason=body.reason, reason_label=REASONS[body.reason], note=body.note,
                 by=user.get('username') or 'Operator', user_id=user.get('id'), at=datetime.now(timezone.utc).isoformat(),
                 original_grams=original, corrected_grams=corrected if quantity_changed else original, before=before, after=after,
                 quantity_changed=quantity_changed, job_id=row['scheduled_job_id'], printer_id=row['printer_id'],
                 job_attempt=attempt_seq(sqlalchemy_runner(db), job['id']) if job else None,
                 accounting={str(a['id']): [a['accounting_state'], a['accounting_reason']] for a in archives},
                 material_snapshot=row['spool_snapshot'],
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
    warnings = []
    # Restore quantities only while the entire material image still belongs to
    # this correction. Later accounting must never be unwound by a status edit.
    restore_material = saved['quantity_changed']
    if saved['quantity_changed']:
        current = {str(a['id']): _image(a, ARCHIVE_FIELDS) for a in archives}
        same_archives = current.keys() == saved['after']['archives'].keys()
        same_material = same_archives and all(
            all(values.get(k) == saved['after']['archives'][aid].get(k) for k in ARCHIVE_FIELDS if k != 'status')
            for aid, values in current.items())
        accounting = {str(a['id']): [a['accounting_state'], a['accounting_reason']] for a in archives}
        restore_material = (same_material and row['filament_used_g'] == saved['after']['print']['filament_used_g']
                            and row['spool_snapshot'] == saved.get('material_snapshot', row['spool_snapshot'])
                            and accounting == saved.get('accounting', accounting))
        if restore_material:
            try:
                _quantity_allowed(db, row, archives)
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                restore_material = False
        if not restore_material:
            warnings.append('Material records or accounting changed after the correction. Current grams, costs and inventory were left unchanged.')
    elif (_image(row, PRINT_FIELDS) != saved['after']['print'] or
          {str(a['id']): _image(a, ARCHIVE_FIELDS) for a in archives} != saved['after']['archives']):
        warnings.append('Later print or archive changes were preserved; only their outcomes were restored to cancelled.')
    if saved['before']['job'] is not None:
        latest = db.execute(text('SELECT MAX(id) FROM print_jobs WHERE scheduled_job_id=:job'), {'job': saved['job_id']}).scalar()
        if (job and job['id'] == saved['job_id'] and latest == row['id']
                and job['status'] == 'failed'
                and attempt_seq(sqlalchemy_runner(db), job['id']) == saved.get('job_attempt', 0)):
            values = _image(job, JOB_FIELDS)
            kept = [key for key in JOB_FIELDS if values[key] != saved['after']['job'][key]]
            for key in JOB_FIELDS:
                if key not in kept:
                    values[key] = saved['before']['job'][key]
            # Education admission may have changed since marking. Preserve the
            # new workflow, but still let the physical correction be undone.
            try:
                assert_generic_job_mutation_allowed(db, job['id'])
            except HTTPException:
                warnings.append('The linked job is now managed by another workflow; its outcome and fields were left unchanged.')
            else:
                _write_job(db, job['id'], values)
                if kept:
                    warnings.append('The linked job was restored to cancelled; later edits were kept: ' + ', '.join(kept) + '.')
        else:
            warnings.append('The linked job was deleted, requeued or changed to a later outcome; it was left unchanged.')
    values = dict(_image(row, PRINT_FIELDS), status='cancelled')
    if restore_material:
        values['filament_used_g'] = saved['before']['print']['filament_used_g']
    _write_print(db, row['id'], values)
    for archive in archives:
        values = dict(_image(archive, ARCHIVE_FIELDS), status='cancelled')
        if restore_material:
            values.update(saved['before']['archives'][str(archive['id'])])
        _write_archive(db, archive['id'], values)
    saved.update(active=False, undone_by=user.get('username') or 'Operator', undone_by_id=user.get('id'), undone_at=datetime.now(timezone.utc).isoformat(), undo_warnings=warnings)
    _save(db, row['id'], saved)
    log_audit(db, 'print.failure_undone', 'print_job', row['id'], saved)
    return {'id': row['id'], 'status': 'cancelled', 'warnings': warnings}
