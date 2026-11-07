"""Persisted controlled-part restrictions, independent of entitlement expiry.

Organization checks remain the caller's responsibility. These additional checks
never grant access and never grant a platform-admin or legacy API-key bypass.
Printer groups are the same exact, case-sensitive tags used by the scheduler.
"""
import json
import re
from fastapi import HTTPException
from sqlalchemy import text
from core.controlled_policy_clock import deadline_sql

# Use the same Education/explicit/model/latest-variant resolution as dispatch.
def selected_file_sql(job_expression):
    from core.dispatch_payload_selection import SELECTED_PRINT_FILE_ID_SQL
    from core.controlled_sql import inner_alias
    job, model, submission = (inner_alias(name, job_expression) for name in ('cj', 'cm', 'cs'))
    candidate = inner_alias('candidate', job_expression)
    aliases = {'j': job, 'm': model, 's': submission}
    selected = re.sub(r'\b(j|m|s)\.', lambda match: aliases[match[1]] + '.', SELECTED_PRINT_FILE_ID_SQL)
    selected = re.sub(r'\bcandidate\b', candidate, selected)
    return (f'SELECT {selected} FROM jobs {job} LEFT JOIN models {model} ON {model}.id={job}.model_id '
            f'LEFT JOIN education_submissions {submission} ON {submission}.job_id={job}.id WHERE {job}.id=' + job_expression)


JOB_FILES = selected_file_sql(':id')


def policies(db, kind, identifier):
    if identifier is None:
        return []
    # This must precede media-link lookup: unmarked print-job lists stay O(1).
    if not any_marks(db):
        history = tombstone_job_ids(db)
        if not history or kind in ('file', 'print_file', 'model') or (kind == 'job' and identifier not in history):
            return []
    if kind in ('print_job', 'archive'):
        query = ('SELECT scheduled_job_id FROM print_jobs WHERE id=:id' if kind == 'print_job' else 'SELECT job_id FROM print_archives WHERE id=:id')
        linked = db.execute(text(query), {'id': identifier}).scalar()
        return policies(db, 'job', linked)
    if kind == 'print_file':
        kind = 'file'
    if kind == 'file':
        query = _file_rules(':id')
    elif kind == 'job':
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- policy SQL uses fixed table names, validated aliases and source-owned retention fragments; resource IDs are bound
        direct = [dict(r) for r in db.execute(text(
            'SELECT label,audience_id,printer_tag,retention_days,marked_at,purge_previewed_at,' + deadline_sql('jobs', 'job_id') + ' FROM controlled_jobs cp WHERE job_id=:id'), {'id': identifier}).mappings()]
        linked = db.execute(text(JOB_FILES), {'id': identifier}).scalar()
        combined = direct + policies(db, 'file', linked)
        if not db.execute(text('SELECT 1 FROM jobs WHERE id=:id'), {'id': identifier}).first():
            from core.controlled_history import tombstone_policies
            combined += tombstone_policies(db, identifier)
        return list({tuple(sorted(row.items())): row for row in combined}.values())
    elif kind == 'model':
        ids = db.execute(text('SELECT id FROM print_files WHERE model_id=:id UNION SELECT print_file_id FROM models WHERE id=:id'), {'id': identifier}).scalars()
        return [policy for fid in ids if fid is not None for policy in policies(db, 'file', fid)]
    elif kind in ('printer', 'printer_names'):
        ids = db.execute(text("SELECT id FROM jobs WHERE printer_id=:id AND status IN ('printing','paused') "
                              "UNION SELECT job_id FROM printer_dispatch_claims WHERE printer_id=:id "
                              "AND state IN ('preparing','adapter_entered','accepted','uncertain')"), {'id': identifier}).scalars()
        rules = [policy for jid in ids if jid is not None for policy in policies(db, 'job', jid)]
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- policy SQL uses fixed table names, validated aliases and source-owned retention fragments; resource IDs are bound
        prints = db.execute(text("SELECT id FROM print_jobs WHERE printer_id=:id "
            "AND (status IN ('running','printing','paused') " +
            ("OR id=(SELECT id FROM print_jobs WHERE printer_id=:id ORDER BY started_at DESC,id DESC LIMIT 1) "
             if kind == 'printer_names' else '') + ")"), {'id': identifier}).scalars()
        return rules + [policy for pid in prints for policy in policies(db, 'print_job', pid)]
    else:
        raise ValueError('Unknown controlled resource')
    return [dict(r) for r in db.execute(text(query), {'id': identifier}).mappings()]


def member(db, actor, group):
    if not actor or actor.get('_auth_kind') == 'legacy_global_api_key':
        return False
    return db.execute(text('SELECT 1 FROM users u JOIN controlled_audience_members m ON m.user_id=u.id WHERE u.id=:id AND m.audience_id=:group AND u.is_active IS TRUE'),
                      {'id': actor.get('id'), 'group': group}).first() is not None


def allowed(db, kind, identifier, actor):
    return all(member(db, actor, row['audience_id']) for row in policies(db, kind, identifier))


def refuse(db, code, kind, identifier, *, status_code=403):
    # This boundary must run before business writes. A refusal must survive the
    # request rollback, but must never commit an unrelated staged mutation.
    from core.audit import audit_download
    if db.new or db.dirty or db.deleted:
        raise RuntimeError('Controlled authorization must precede mutations')
    audit_download(db, 'controlled_part.refused', kind, identifier, {'reason': code})
    raise HTTPException(status_code, {'code': code, 'message': 'Controlled part access refused'})


def assert_access(db, kind, identifier, actor):
    if not allowed(db, kind, identifier, actor):
        refuse(db, 'controlled_part_group_required', kind, identifier)


def dispatch_refusal(db, job_id, printer_id):
    rules = policies(db, 'job', job_id)
    if not rules:
        return None
    from license_manager import get_license
    license_info = get_license()
    if not license_info.valid or license_info.tier != 'enterprise' or not license_info.has_feature('controlled_parts'):
        return 'controlled_part_feature_not_licensed'
    raw = db.execute(text('SELECT tags FROM printers WHERE id=:id'), {'id': printer_id}).scalar()
    try:
        tags = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        tags = None
    if not isinstance(tags, list) or any(r['printer_tag'] not in tags for r in rules):
        return 'controlled_part_printer_group_required'
    return None


def _file_rules(expression):
    from core.controlled_sql import inner_alias
    cp, marked, candidate = (inner_alias(name, expression) for name in ('cp', 'marked', 'candidate'))
    fields = ','.join(cp + '.' + name for name in
                     ('label', 'audience_id', 'printer_tag', 'retention_days', 'marked_at', 'purge_previewed_at'))
    matching = (f' JOIN print_files {candidate} ON ({candidate}.id={marked}.id OR '
                f'({candidate}.stored_path IS NOT NULL AND {candidate}.stored_path={marked}.stored_path)) '
                f'WHERE {candidate}.id=' + expression)
    return (f'SELECT {fields},' + deadline_sql('files', 'file_id', cp) +
            f' FROM controlled_files {cp} JOIN print_files {marked} ON {marked}.id={cp}.file_id' + matching +
            f' UNION ALL SELECT {fields},' + deadline_sql('jobs', 'job_id', cp) +
            f' FROM controlled_jobs {cp} JOIN print_files {marked} ON {marked}.id=('
            + selected_file_sql(cp + '.job_id') + ')' + matching)


def _audience(actor, rules):
    from core.controlled_sql import inner_alias
    cp, cu, cam = (inner_alias(name, rules) for name in ('cp', 'cu', 'cam'))
    uid = actor.get('id') if actor and actor.get('_auth_kind') != 'legacy_global_api_key' else None
    condition = ('NOT EXISTS (SELECT 1 FROM (' + rules + f') {cp} WHERE NOT EXISTS '
                 f'(SELECT 1 FROM users {cu} JOIN controlled_audience_members {cam} ON {cam}.user_id={cu}.id '
                 f'WHERE {cu}.id=:_controlled_user AND {cu}.is_active IS TRUE AND {cam}.audience_id={cp}.audience_id))')
    return condition, {'_controlled_user': uid}


def file_visibility(actor, expression):
    if expression not in ('a.print_file_id', 'f.id', 'print_files.id'):
        raise ValueError('Untrusted file expression')
    return _audience(actor, _file_rules(expression))


def job_visibility(actor, alias='jobs'):
    if alias not in ('jobs', 'j', 'a'):
        raise ValueError('Untrusted job alias')
    job_id = alias + ('.job_id' if alias == 'a' else '.id')
    file_id = '(' + selected_file_sql(job_id) + ')'
    rules = ('SELECT label,audience_id,printer_tag,retention_days,marked_at,purge_previewed_at,' + deadline_sql('jobs', 'job_id') + ' FROM controlled_jobs cp WHERE job_id=' + job_id +
             ' UNION ALL ' + _file_rules(file_id))
    return _audience(actor, rules)


def path_allowed(db, path, actor):
    if not path:
        return True
    ids = db.execute(text('SELECT id FROM print_files WHERE stored_path=:path'), {'path': path}).scalars()
    return all(allowed(db, 'file', identifier, actor) for identifier in ids)


def assert_full_export(db, actor):
    groups = db.execute(text('SELECT audience_id FROM controlled_files UNION SELECT audience_id FROM controlled_jobs')).scalars()
    if any(not member(db, actor, group) for group in groups):
        refuse(db, 'controlled_part_export_group_required', 'system', None)


def historical_camera_allowed(db, printer_id, actor):
    # Legacy Vigil frames have no immutable job binding. Restrict their printer's
    # historical image store when any retained controlled job used that printer.
    ids = db.execute(text('SELECT id FROM jobs WHERE printer_id=:id'), {'id': printer_id}).scalars()
    if not all(allowed(db, 'job', jid, actor) for jid in ids):
        return False
    prints = db.execute(text('SELECT id FROM print_jobs WHERE printer_id=:id'), {'id': printer_id}).scalars()
    return all(allowed(db, 'print_job', pid, actor) for pid in prints)


def assert_group_deletable(db, identifier):
    used = db.execute(text('SELECT 1 FROM controlled_files c JOIN print_files f ON f.id=c.file_id WHERE f.org_id=:id UNION '
                           'SELECT 1 FROM controlled_jobs c JOIN jobs j ON j.id=c.job_id WHERE j.charged_to_org_id=:id'), {'id': identifier}).first()
    if used:
        refuse(db, 'controlled_part_group_in_use', 'group', identifier)


def audit_visibility(actor, db):
    """Keep the global audit usable without exposing protected entity details."""
    jobs, values = job_visibility(actor, 'j')
    files, _ = file_visibility(actor, 'f.id')
    tombstones, _ = _audience(actor, 'SELECT audience_id FROM controlled_job_tombstones cp WHERE cp.job_id=audit_logs.entity_id')
    return ("(audit_logs.entity_type IS NULL OR audit_logs.entity_id IS NULL OR ("
            "(audit_logs.entity_type NOT IN ('job','file','print_file','model')) OR "
            "(audit_logs.entity_type='job' AND NOT EXISTS (SELECT 1 FROM jobs j "
            "WHERE j.id=audit_logs.entity_id AND NOT (" + jobs + ")) AND " + tombstones + ") OR "
            "(audit_logs.entity_type IN ('file','print_file') AND NOT EXISTS (SELECT 1 FROM print_files f "
            "WHERE f.id=audit_logs.entity_id AND NOT (" + files + "))) OR "
            "(audit_logs.entity_type='model' AND NOT EXISTS (SELECT 1 FROM print_files f "
            "WHERE (f.model_id=audit_logs.entity_id OR f.id=(SELECT print_file_id FROM models "
            "WHERE id=audit_logs.entity_id)) AND NOT (" + files + ")))))"), values


def any_marks(db):
    """One request-local existence query; never cache membership grants."""
    from core.controlled_cache import request_cache
    cache = request_cache(db)
    if cache is not None and '_controlled_any' in cache:
        return cache['_controlled_any']
    result = db.execute(text('SELECT 1 FROM controlled_files UNION ALL SELECT 1 FROM controlled_jobs LIMIT 1')).first() is not None
    if cache is not None:
        cache['_controlled_any'] = result
    return result


def model_visibility(actor):
    files, values = file_visibility(actor, 'f.id')
    return ('NOT EXISTS (SELECT 1 FROM print_files f WHERE (f.model_id=models.id OR f.id=models.print_file_id) '
            'AND NOT (' + files + '))'), values


def any_read_restrictions(db):
    """Historical redaction and export gates stay active after live marks are gone."""
    if any_marks(db):
        return True
    from core.controlled_cache import request_cache
    cache = request_cache(db)
    if cache is not None and '_controlled_history' in cache:
        return cache['_controlled_history']
    from core.controlled_history import retained_media_sql
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- policy SQL uses fixed table names, validated aliases and source-owned retention fragments; resource IDs are bound
    result = db.execute(text("""SELECT 1 FROM controlled_job_tombstones t
        JOIN controlled_audiences a ON a.id=t.audience_id WHERE """ + retained_media_sql('t') + """
        OR EXISTS (SELECT 1 FROM audit_logs l WHERE l.entity_type='job'
            AND l.entity_id=t.job_id
            AND l.action NOT LIKE 'controlled_part.%') LIMIT 1""")).first() is not None
    if cache is not None:
        cache['_controlled_history'] = result
    return result


def tombstone_job_ids(db):
    """One request-local snapshot; media rules survive even after audit pruning."""
    from core.controlled_cache import request_cache
    cache = request_cache(db)
    if cache is not None and '_controlled_tombstones' in cache:
        return cache['_controlled_tombstones']
    from core.controlled_history import retained_media_sql
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- policy SQL uses fixed table names, validated aliases and source-owned retention fragments; resource IDs are bound
    result = set(db.execute(text("""SELECT DISTINCT t.job_id FROM controlled_job_tombstones t
        WHERE EXISTS (SELECT 1 FROM audit_logs l WHERE l.entity_type='job' AND l.entity_id=t.job_id
            AND l.action NOT LIKE 'controlled_part.%')
        OR """ + retained_media_sql('t'))).scalars())
    if cache is not None:
        cache['_controlled_tombstones'] = result
    return result
