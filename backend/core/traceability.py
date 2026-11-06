"""Attempt-specific traceability. Caller owns authorization and transaction."""
import json
import hashlib
from datetime import datetime, timezone
from sqlalchemy import text
from fastapi import HTTPException
from core.print_attempts import attempt_seq, attempt_reset_at, bound_print_attempt, sqlalchemy_runner


def now():
    return datetime.now(timezone.utc).isoformat()


def rows(db, statement, **params):
    return [dict(r._mapping) for r in db.execute(text(statement), params)]


def one(db, statement, **params):
    result = rows(db, statement, **params)
    return result[0] if result else None


def decoded(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def lock_policy_scope(db, owner):
    if owner:
        return db.execute(text('UPDATE groups SET id=id WHERE id=:id'), {'id':owner}).rowcount == 1
    db.execute(text("INSERT INTO system_config(key,value) VALUES('traceability:policy_lock','0') ON CONFLICT(key) DO NOTHING"))
    db.execute(text("UPDATE system_config SET value=value WHERE key='traceability:policy_lock'"))
    return True


def owned_job(db, job_id, user, *, mutate=False, attempt=None):
    # A no-op write serializes inspection, delivery and reset on the job row.
    # Unlike generic queue visibility, NULL owner does not disclose platform work
    # to a tenant through a detailed manufacturing export.
    job = one(db, 'SELECT * FROM jobs WHERE id=:id', id=job_id)
    platform = user.get('role') == 'admin' and user.get('group_id') is None
    if not job or (not platform and (job['charged_to_org_id'] is None or job['charged_to_org_id'] != user.get('group_id'))):
        raise HTTPException(404, 'Job not found')
    if not one(db, 'SELECT id FROM users WHERE id=:id AND is_active IS TRUE', id=user.get('id')):
        raise HTTPException(401, 'Not authenticated')
    from core.registry import registry
    policy = registry.get_provider('EducationPolicyProvider')
    if policy is None:
        raise HTTPException(503, 'Education policy unavailable')
    policy.assert_generic_job_mutation_allowed(db, job_id)
    if mutate:
        if db.execute(text('UPDATE users SET id=id WHERE id=:id AND is_active IS TRUE'), {'id':user['id']}).rowcount != 1:
            raise HTTPException(401, 'Not authenticated')
        # Match shipping's order -> policy -> job lock order.
        from core.order_production import prepare_job_write
        prepare_job_write(db, [job_id])
        lock_policy_scope(db, job['charged_to_org_id'] or 0)
        db.execute(text('UPDATE jobs SET id=id WHERE id=:id'), {'id': job_id})
        job = one(db, 'SELECT * FROM jobs WHERE id=:id', id=job_id)
        if not job or (not platform and job['charged_to_org_id'] != user.get('group_id')):
            raise HTTPException(404, 'Job not found')
        policy.assert_generic_job_mutation_allowed(db, job_id)
    from core.controlled_parts import assert_access
    assert_access(db, 'job', job_id, user)
    seq = attempt_seq(sqlalchemy_runner(db), job_id) if attempt is None else attempt
    if one(db, 'SELECT job_id FROM traceability_records WHERE job_id=:id AND attempt=:seq', id=job_id, seq=seq):
        return job
    file = one(db, 'SELECT org_id,stored_path FROM print_files WHERE id=:id', id=job['print_file_id'])
    if file and file['org_id'] not in (None, job['charged_to_org_id']):
        raise HTTPException(404, 'Job file not found')
    if file:
        from core.model_preview_access import is_education_owned_file
        alias = one(db, '''SELECT original.id FROM print_files original JOIN education_submissions s
            ON s.print_file_id=original.id WHERE original.stored_path=:path AND :path IS NOT NULL LIMIT 1''', path=file['stored_path'])
        if alias or is_education_owned_file(db, job['print_file_id'], file['stored_path']):
            raise HTTPException(409, 'Education files require the Education workflow')
    if job['status'] != 'completed':
        raise HTTPException(409, 'Traceability requires a completed job')
    return job


def capture(db, job, seq, *, completed_now=False):
    """Retained manufacturing facts, with explicit provenance for mutable metadata.

    No guessed model-to-file binding or currently loaded spool substitution.
    Completion freezes facts; legacy records explicitly identify late capture.
    """
    file = one(db, '''SELECT id,original_filename,filename,file_hash,layer_height,
        nozzle_diameter,layer_count,bed_type,print_time_seconds,filaments_json
        FROM print_files WHERE id=:id''', id=job['print_file_id']) or {}
    printer = one(db, '''SELECT id,name,model,api_type,api_key,nozzle_type,nozzle_diameter,installed_nozzles,tags,org_id,shared
        FROM printers WHERE id=:id''', id=job['printer_id']) or {}
    policy_tags = decoded(printer.get('tags'), [])
    if printer and not printer['shared'] and printer['org_id'] not in (None, job['charged_to_org_id']):
        printer = {}
    serial = None
    if printer.get('api_type') == 'bambu' and printer.get('api_key'):
        from core.crypto import decrypt
        from cryptography.fernet import InvalidToken
        try:
            parts = decrypt(printer['api_key']).split('|')
            serial = parts[0] if len(parts) == 2 else None
        except (ValueError, TypeError, InvalidToken):
            pass
    run = sqlalchemy_runner(db)
    reset = attempt_reset_at(run, job['id'], seq)
    from core.traceability_materials import attempt_usage
    usage = attempt_usage(db, job, seq, reset)
    revision = one(db, 'SELECT id,revision_number FROM model_revisions WHERE id=:id', id=job['model_revision_id'])
    from core.traceability_evidence import attempt_evidence
    evidence = attempt_evidence(db, job, seq, reset, strict=completed_now)
    product = one(db, 'SELECT product_id FROM order_items WHERE id=:id', id=job['order_item_id']) or {}
    return {
        **{k:v for k,v in evidence.items() if k not in ("dispatched_sha256", "hash_source")},
        'captured_after_completion': not completed_now,
        'policy_scope': {'printer_tags': policy_tags, 'product_id': product.get('product_id')},
        'job_id': job['id'], 'attempt': seq, 'outcome': job['status'],
        'file': {'id': job['print_file_id'], 'name': file.get('original_filename') or file.get('filename'),
                 'sha256': evidence['dispatched_sha256'] or job['payload_sha256'] or file.get('file_hash'),
                 'hash_source': evidence['hash_source'] or ('job.payload_sha256' if job['payload_sha256'] else 'print_files.file_hash')},
        'model_revision': revision or {'id': job['model_revision_id'], 'revision_number': None},
        'printer': {'id':job['printer_id'], **{k: printer.get(k) for k in ('name','model')}},
        'printer_serial': serial,
        'nozzle': {'type': printer.get('nozzle_type'), 'diameter': file.get('nozzle_diameter'),
                   'observations': decoded(printer.get('installed_nozzles'), [])},
        'filament': usage,
        'slicer_settings': {k: file.get(k) for k in ('layer_height','layer_count','bed_type','print_time_seconds')},
        'slicer_filaments': decoded(file.get('filaments_json')),
        'timestamps': {k: job[k] for k in ('created_at','actual_start','actual_end')},
        'provenance': {
            'capture': 'completion transaction' if completed_now else 'captured_after_completion: first access to legacy job',
            'file': 'job-bound print file; hash retained by ODIN, not rehashed at export',
            'filament': 'recorded spool usage for this attempt; spool descriptions at capture',
            'printer': 'printer configuration at capture; nozzle diameter from retained slicer file',
            'unknown': 'null means not recorded; no current tray assignment is used as usage evidence',
        },
    }


def snapshot_bytes(snapshot):
    return json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)


def snapshot_digest(snapshot):
    return hashlib.sha256(snapshot_bytes(snapshot).encode()).hexdigest()


def record(db, job, *, attempt=None, completed_now=False):
    seq = attempt_seq(sqlalchemy_runner(db), job['id']) if attempt is None else attempt
    params = {'id': job['id'], 'seq': seq}
    existing = one(db, 'SELECT * FROM traceability_records WHERE job_id=:id AND attempt=:seq', **params)
    if existing is None:
        if seq != attempt_seq(sqlalchemy_runner(db), job['id']) or job['status'] != 'completed':
            raise HTTPException(404, 'No retained traceability for this attempt')
        snapshot = capture(db, job, seq, completed_now=completed_now)
        db.execute(text('''INSERT INTO traceability_records(job_id,attempt,snapshot,snapshot_sha256,captured_at)
            VALUES(:id,:seq,:snapshot,:digest,:at) ON CONFLICT(job_id,attempt) DO NOTHING'''),
            {**params, 'snapshot':snapshot_bytes(snapshot), 'digest':snapshot_digest(snapshot), 'at':now()})
        existing = one(db, 'SELECT * FROM traceability_records WHERE job_id=:id AND attempt=:seq', **params)
    return existing


def inspection_required(db, job, retained=None):
    policies = rows(db, 'SELECT target_kind,target_key FROM traceability_policies WHERE owner_id=:owner AND required=1', owner=job['charged_to_org_id'] or 0)
    scope = decoded((retained or record(db, job))['snapshot'], {}).get('policy_scope', {})
    return any((p['target_kind'] == 'printer_tag' and p['target_key'] in scope.get('printer_tags', [])) or
               (p['target_kind'] == 'product' and p['target_key'] == str(scope.get('product_id'))) for p in policies)


def binding_drift(job, snapshot):
    return (snapshot.get('file', {}).get('id') != job['print_file_id'] or
            snapshot.get('printer', {}).get('id') != job['printer_id'] or
            bool(job['payload_sha256'] and snapshot.get('file', {}).get('sha256') != job['payload_sha256']))


def render_record(db, job, retained):
    result = decoded(retained['snapshot'], {})
    result['captured_at'] = retained['captured_at']
    result['inspection'] = {k: retained[k] for k in ('inspection_result','inspection_notes','inspected_by','inspected_at')}
    result['delivery'] = {k: retained[k] for k in ('delivered_by','delivered_at')}
    result['snapshot_sha256'] = retained['snapshot_sha256']
    result['inspection_required'] = inspection_required(db, job, retained)
    result['warnings'] = ['traceability_evidence_changed: live job binding differs'] if binding_drift(job, result) else []
    if retained['attempt'] != attempt_seq(sqlalchemy_runner(db), job['id']):
        result['warnings'].append('Historical attempt; job has been reset')
    current_printer = one(db, 'SELECT name,tags FROM printers WHERE id=:id', id=result.get('printer', {}).get('id'))
    if not current_printer or current_printer['name'] != result.get('printer', {}).get('name') or decoded(current_printer['tags'], []) != result.get('policy_scope', {}).get('printer_tags', []):
        result['warnings'].append('Printer metadata changed or printer deleted')
    # Export only curated lifecycle events; raw audit details may contain other
    # users' identifiers and are available through their own authorized surface.
    result.pop('audit_trail', None)
    from core.traceability_materials import rendered_usage
    result['filament'] = rendered_usage(db, job, result)
    result['provenance']['filament'] = ('Captured usage plus append-only, attempt-bound ledger rows recorded after capture; '
        'late descriptions use captured start evidence when available, otherwise current spool metadata. '
        'snapshot_sha256 covers the stored snapshot, excluding later ledger evidence.')
    # Resolve frozen operator IDs against current, erasure-aware identities.
    result['operators'] = [dict(id=uid, username=(one(db, 'SELECT username FROM users WHERE id=:id', id=uid) or {}).get('username')) for uid in result.get('operator_ids', [])]
    result['timelapses'] = rows(db, '''SELECT t.id,t.print_job_id FROM timelapses t JOIN print_jobs p ON p.id=t.print_job_id
        WHERE p.scheduled_job_id=:id AND t.status='ready' AND t.retirement_operation_id IS NULL
        ORDER BY t.id''', id=job['id'])
    result['timelapses'] = [t for t in result['timelapses'] if bound_print_attempt(sqlalchemy_runner(db), t['print_job_id'], job['id']) == retained['attempt']]
    for item in result['timelapses']:
        item['url'] = '/api/timelapses/'+str(item['id'])+'/video'
    return result
