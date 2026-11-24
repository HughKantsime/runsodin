"""Reversible corrections of finished physical prints. No terminal event delivery.

The existing provenance store holds the original and corrected outcomes.
Recorded material, costs and inventory are never changed.
"""
import json
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import bindparam, text

from core.audit import log_audit
from core.controlled_parts import allowed
from core.interfaces.education_policy import is_education_reserved_filename
from core.order_production import ProductionLinkageChanged, prepare_job_write, prepare_print_job_write
from core.print_attempts import attempt_seq, sqlalchemy_runner
from modules.jobs.write_access import is_job_submitter
from core.rbac import check_org_access, check_org_write, require_org_write
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
ARCHIVE_FIELDS = ('status',)
PRINT_FIELDS = ('status',)
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
                and row['printer_id'] == saved['printer_id']
                and str(row['started_at']) == saved['started_at'])


def _can_correct(db, user):
    if not user or user.get('role') not in ('operator', 'admin'):
        return False
    # Use the endpoint's permission checker, including the platform-admin
    # exception and scoped-token policy; do not duplicate the permission map.
    from core.rbac import require_action
    try:
        require_action('jobs.edit', scope='write:jobs')(None, user, db)
    except HTTPException:
        return False
    return True


def annotate(db, rows, *, archive=False, user=None):
    """Only call after the caller has applied the source record's audience rules."""
    ids = sorted({row.get('print_job_id') if archive else row['id'] for row in rows} - {None})
    values = {}
    authorized = set()
    if ids:
        can_correct = _can_correct(db, user)
        from core.controlled_history import media_visibility
        from modules.archives.services import archive_visibility
        controlled, parameters = media_visibility(user, 'p.scheduled_job_id')
        audience, bindings = archive_visibility(user)
        parameters.update(bindings)
        # One bounded batch for provenance, immutable print identity and read
        # audience. Job mutation eligibility is deliberately not a read policy.
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- the joined fragments come from media_visibility and archive_visibility, built from code text with bound values
        query = text('SELECT p.*,r.org_id AS printer_org,j.charged_to_org_id AS job_org,j.submitted_by,c.value '
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
                if can_correct and (row['scheduled_job_id'] is None or check_org_write(
                        user, row['job_org'], operate=is_job_submitter(user, row['submitted_by']))):
                    authorized.add(row['id'])
                saved = decoded(row['value']) if row['value'] else None
                if saved and saved.get('active') and matches_print(row, saved):
                    values[row['id']] = {k: saved[k] for k in ('reason', 'reason_label', 'note', 'by', 'at')}
    return [dict(row, failure_correction=values.get(row.get('print_job_id') if archive else row['id']),
                 failure_correction_allowed=(row.get('print_job_id') if archive else row['id']) in authorized) for row in rows]


def _load(db, identifier, user):
    row = db.execute(text('SELECT p.*,r.org_id AS printer_org FROM print_jobs p JOIN printers r ON r.id=p.printer_id WHERE p.id=:id'),
                     {'id': identifier}).mappings().first()
    if not row or not check_org_access(user, row['printer_org']) or not allowed(db, 'print_job', identifier, user):
        raise HTTPException(404, 'Print not found')
    job = None
    if row['scheduled_job_id'] is not None:
        job = db.execute(text('SELECT * FROM jobs WHERE id=:id'), {'id': row['scheduled_job_id']}).mappings().first()
        if job and not check_org_access(user, job['charged_to_org_id']):
            raise HTTPException(404, 'Print not found')
        if job:
            assert_generic_job_mutation_allowed(db, job['id'])
    if any(is_education_reserved_filename(row[k]) for k in ('filename', 'job_name')):
        fail('This print must be managed through its Education workflow.')
    archives = db.execute(text('SELECT * FROM print_archives WHERE print_job_id=:id ORDER BY id'), {'id': identifier}).mappings().all()
    for archive in archives:
        assert_archive_access(db, archive['id'], user)
    from core.interfaces.education_policy import generic_archive_audience_sql
    # Includes archived Education content whose original job was deleted.
    education = generic_archive_audience_sql()
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- the joined fragment is generic_archive_audience_sql(), fixed code text; the id is bound
    if db.execute(text('SELECT 1 FROM print_archives a WHERE a.print_job_id=:id AND NOT COALESCE((' + education + '), FALSE)'), {'id': identifier}).first():
        fail('This print must be managed through its Education workflow.')
    if job:
        require_org_write(user, job['charged_to_org_id'], 'job',
                          operate=is_job_submitter(user, job['submitted_by']))
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


def _image(row, fields):
    return {key: row[key].isoformat(sep=' ') if isinstance(row[key], datetime) else row[key] for key in fields}


def _write_print(db, identifier, values):
    db.execute(text('UPDATE print_jobs SET status=:status WHERE id=:id'), dict(values, id=identifier))


def _write_archive(db, identifier, values, *, expected=None, print_id=None):
    statement = 'UPDATE print_archives SET status=:status WHERE id=:id'
    if expected is not None:
        statement += ' AND status=:expected AND print_job_id=:print_id'
    return db.execute(text(statement), dict(values, id=identifier, expected=expected, print_id=print_id)).rowcount


def change(db, identifier, user, body=None):
    """Caller commits/rolls back the correction, provenance and audit together."""
    if user.get('role') not in ('operator', 'admin'):
        raise HTTPException(403, 'Only operators and administrators can correct a print.')
    row, _, _ = _load(db, identifier, user)
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
    row, job, archives = _load(db, identifier, user)
    saved = record(db, identifier)
    if body is None:
        return _undo(db, row, job, archives, saved, user)
    _settled(db, row, job, archives)
    if saved and saved.get('active'):
        fail('This print is already marked as failed. Use Back to cancelled before making another correction.')
    if row['status'] != 'cancelled':
        fail('Only a finished, cancelled print can be marked as failed.')
    before = {'print': _image(row, PRINT_FIELDS), 'archives': {str(a['id']): _image(a, ARCHIVE_FIELDS) for a in archives}, 'job': None}
    after = {'print': dict(before['print'], status='failed'), 'archives': {k: dict(v, status='failed') for k, v in before['archives'].items()}, 'job': None}
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
                 before=before, after=after, job_id=row['scheduled_job_id'], printer_id=row['printer_id'],
                 job_attempt=attempt_seq(sqlalchemy_runner(db), job['id']) if job else None,
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
    if saved['before']['job'] is not None:
        latest = db.execute(text('SELECT MAX(id) FROM print_jobs WHERE scheduled_job_id=:job'), {'job': saved['job_id']}).scalar()
        if (job and job['id'] == saved['job_id'] and latest == row['id']
                and job['status'] == 'failed'
                and attempt_seq(sqlalchemy_runner(db), job['id']) == saved['job_attempt']):
            values = _image(job, JOB_FIELDS)
            kept = [key for key in JOB_FIELDS if values[key] != saved['after']['job'][key]]
            for key in JOB_FIELDS:
                if key not in kept:
                    values[key] = saved['before']['job'][key]
            _write_job(db, job['id'], values)
            if kept:
                warnings.append(f"Job {job['id']} was restored to cancelled; later edits were kept: " + ', '.join(kept) + '.')
        else:
            warnings.append(f"Job {saved['job_id']} was deleted, requeued or changed to a later outcome; it was left unchanged.")
    status = row['status']
    if status == saved['after']['print']['status']:
        status = saved['before']['print']['status']
        _write_print(db, row['id'], {'status': status})
    else:
        warnings.append(f"Print {row['id']} has a later outcome ({status}); it was left unchanged.")
    seen = set()
    for archive in archives:
        aid = str(archive['id'])
        seen.add(aid)
        if aid not in saved['after']['archives']:
            warnings.append(f"Archive {aid} was added after the correction; it was left unchanged.")
        elif archive['status'] != saved['after']['archives'][aid]['status']:
            warnings.append(f"Archive {aid} has a later outcome ({archive['status']}); it was left unchanged.")
        else:
            changed = _write_archive(db, archive['id'], saved['before']['archives'][aid],
                                     expected=saved['after']['archives'][aid]['status'], print_id=row['id'])
            if changed != 1:
                warnings.append(f"Archive {aid} changed during undo; it was left unchanged.")
    for aid in sorted(saved['after']['archives'].keys() - seen):
        warnings.append(f"Archive {aid} was deleted or moved after the correction; it was left unchanged.")
    saved.update(active=False, undone_by=user.get('username') or 'Operator', undone_by_id=user.get('id'), undone_at=datetime.now(timezone.utc).isoformat(), undo_warnings=warnings)
    _save(db, row['id'], saved)
    log_audit(db, 'print.failure_undone', 'print_job', row['id'], saved)
    return {'id': row['id'], 'status': status, 'warnings': warnings}
