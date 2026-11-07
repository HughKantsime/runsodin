"""Redact known fields of identified resources, never arbitrary matching text.

Identity sets are computed once per request from the marked source graph. Text
belonging to unrelated resources (even identical names) remains unchanged.
"""
from sqlalchemy import text
from core.controlled_parts import any_marks, selected_file_sql

REDACTED = '[controlled]'


def printer_status(db, printer_id, actor, value, *, force=False):
    from core.controlled_parts import allowed
    if not force and allowed(db, 'printer_names', printer_id, actor):
        return value
    fields = {'job_name', 'filename', 'subtask_name', 'current_print', 'current_print_display_name',
              'display_name', 'file', 'gcode_file', 'title', 'message', 'notes'}
    def redact(item):
        if isinstance(item, dict):
            return {key: REDACTED if key in fields else redact(val) for key, val in item.items()}
        if isinstance(item, list):
            return [redact(v) for v in item]
        return item
    return redact(value)


def denied_ids(db, actor):
    """Bounded projections: no names and no Python full-table scans."""
    uid = actor.get('id') if actor and actor.get('_auth_kind') != 'legacy_global_api_key' else None
    from core.controlled_cache import request_cache
    cache = request_cache(db)
    cache_key = ('_controlled_ids', uid)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    membership = ('NOT EXISTS (SELECT 1 FROM controlled_audience_members cm JOIN users u ON u.id=cm.user_id '
                  'WHERE cm.audience_id=c.audience_id AND u.id=:uid AND u.is_active IS TRUE)')
    graph = ('WITH denied_jobs AS (SELECT c.job_id AS id FROM controlled_jobs c WHERE ' + membership + '), '
             'sources AS (SELECT c.file_id AS id FROM controlled_files c WHERE ' + membership +
             ' UNION SELECT (' + selected_file_sql('dj.id') + ') FROM denied_jobs dj), '
             'files AS (SELECT f.id,f.model_id FROM sources s JOIN print_files marked ON marked.id=s.id '
             'JOIN print_files f ON f.id=marked.id OR (marked.stored_path IS NOT NULL AND f.stored_path=marked.stored_path)), '
             'models_denied AS (SELECT model_id AS id FROM files WHERE model_id IS NOT NULL UNION '
             'SELECT m.id FROM models m JOIN files f ON f.id=m.print_file_id), '
             'jobs_denied AS (SELECT id FROM denied_jobs UNION SELECT c.job_id FROM controlled_job_tombstones c WHERE ' + membership + ' AND NOT EXISTS (SELECT 1 FROM jobs WHERE id=c.job_id) UNION SELECT j.id FROM jobs j '
             'WHERE (' + selected_file_sql('j.id') + ') IN (SELECT id FROM files)) ')
    media_queries = []
    for kind, table, link in (('print_job', 'print_jobs', 'scheduled_job_id'),
                              ('archive', 'print_archives', 'job_id'),
                              ('timelapse', 'timelapses', 'print_job_id')):
        media_queries.append(f"SELECT '{kind}',id FROM {table} WHERE {link} IN (SELECT id FROM jobs_denied)")
    query = graph + ("SELECT 'file',id FROM files UNION SELECT 'model',id FROM models_denied "
                     "UNION SELECT 'job',id FROM jobs_denied "
                     "UNION SELECT 'product',product_id FROM product_components WHERE model_id IN (SELECT id FROM models_denied) "
                     "UNION SELECT 'preset',id FROM print_presets WHERE model_id IN (SELECT id FROM models_denied) "
                     ' UNION ' + ' UNION '.join(media_queries))
    result = {kind: set() for kind in ('job', 'file', 'model', 'product', 'preset', 'print_job', 'archive', 'timelapse')}
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- query combines fixed resource tables and selected_file_sql fragments; the actor ID is bound
    for kind, identifier in db.execute(text(query), {'uid': uid}):
        result[kind].add(identifier)
    result['print_file'] = result['file']
    if cache is not None:
        cache[cache_key] = result
    return result


FIELDS = {
    'job': {'item_name', 'job_name', 'name', 'notes', 'display_name'},
    'print_job': {'job_name', 'filename', 'display_name', 'notes'},
    'file': {'filename', 'original_filename', 'project_name', 'name'},
    'model': {'name', 'model_name', 'description'},
    'product': {'name', 'product_name', 'sku', 'product_sku', 'description'},
    'archive': {'print_name', 'notes'},
    'alert': {'title', 'message', 'job_name'},
    'timelapse': {'job_name', 'display_name'},
    'preset': {'name', 'item_name', 'notes'},
}
COLLECTIONS = {'jobs': 'job', 'print_jobs': 'print_job', 'files': 'file', 'print_files': 'file',
               'models': 'model', 'products': 'product', 'components': 'component', 'presets': 'preset',
               'jobs_submitted': 'job', 'jobs_charged': 'job', 'print_archives': 'archive',
               'uploaded_print_files': 'file', 'alerts': 'alert', 'timelapses': 'timelapse'}
REFERENCES = {'job_id': 'job', 'scheduled_job_id': 'job', 'print_job_id': 'print_job',
              'model_id': 'model', 'file_id': 'file', 'print_file_id': 'file', 'product_id': 'product'}


def redact_export(db, actor, payload, *, resource_kind=None):
    denied = denied_ids(db, actor)
    from core.controlled_audit import audit_allowed
    def walk(value, context=None):
        if isinstance(value, list):
            return [walk(v, context) for v in value]
        if not isinstance(value, dict):
            return value
        result = {k: walk(v, COLLECTIONS.get(k)) for k, v in value.items()}
        kind = value.get('entity_type') or value.get('resource_type')
        identifier = value.get('entity_id', value.get('resource_id'))
        if kind and (not audit_allowed(db, actor, 'job', identifier, value.get('id')) if kind == 'job' else identifier in denied.get(kind, ())):
            for key in ('details', 'reason'):
                if key in result:
                    result[key] = REDACTED
        restricted = set()
        if context and value.get('id') in denied.get(context, ()):
            restricted.add(context)
        for key, ref_kind in REFERENCES.items():
            if context == 'timelapse' and key == 'print_job_id':
                ref_kind = 'job'
            if value.get(key) in denied.get(ref_kind, ()):
                restricted.add(ref_kind)
        # A job's own title is controlled when its selected file/model is controlled.
        if context in ('job', 'archive', 'alert', 'timelapse') and restricted:
            restricted.add(context)
        for ref_kind in restricted:
            for key in FIELDS.get(ref_kind, ()):
                if key in result:
                    result[key] = REDACTED
        return result
    return walk(payload, resource_kind)
