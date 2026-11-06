"""Shared detail-route guard after authentication, before any handler mutation."""
from core.controlled_parts import assert_access


def authorize_request(db, request, actor):
    if request is None or not actor:
        return
    for name, kind in (('job_id', 'job'), ('model_id', 'model'), ('file_id', 'file')):
        identifier = request.path_params.get(name)
        if identifier is not None:
            try:
                identifier = int(identifier)
            except (ValueError, TypeError):
                continue  # FastAPI will return its normal validation error.
            from core.rbac import check_org_access
            from sqlalchemy import text
            table, column = {'job': ('jobs', 'charged_to_org_id'), 'model': ('models', 'org_id'),
                             'file': ('print_files', 'org_id'), 'printer': ('printers', 'org_id')}[kind]
            shared = ', shared' if kind == 'printer' else ''
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table, owner column and shared projection come from the closed resource-kind mapping; id is bound
            owner = db.execute(text(f'SELECT {column}{shared} FROM {table} WHERE id=:id'), {'id': identifier}).first()
            if owner is not None and (check_org_access(actor, owner[0]) or kind == 'printer' and owner[1] in (True, 1)):
                assert_access(db, kind, identifier, actor)


def authorize_references(db, actor, body):
    if isinstance(body, list):
        for value in body:
            authorize_references(db, actor, value)
    elif isinstance(body, dict):
        for name, kind in (('job_id','job'), ('model_id','model'), ('file_id','file'), ('print_file_id','file')):
            identifier = reference_id(body.get(name))
            if type(identifier) is int and identifier > 0:
                assert_access(db, kind, identifier, actor)

        for name, kind in (('job_ids', 'job'), ('file_ids', 'file'), ('model_ids', 'model')):
            identifiers = body.get(name)
            if isinstance(identifiers, list):
                for raw_identifier in identifiers:
                    identifier = reference_id(raw_identifier)
                    if type(identifier) is int and identifier > 0:
                        assert_access(db, kind, identifier, actor)
        for value in body.values():
            if isinstance(value, (dict, list)):
                authorize_references(db, actor, value)


def guard_source_changes(db, request, actor, body=None):
    """Controlled sources must be explicitly released before replacement/deletion."""
    if request.method not in ('DELETE', 'POST', 'PUT', 'PATCH'):
        return
    from core.controlled_parts import policies, refuse
    for key, kind in (('job_id', 'job'), ('model_id', 'model'), ('file_id', 'file')):
        identifier = request.path_params.get(key)
        if identifier is None:
            continue
        try:
            identifier = int(identifier)
        except (ValueError, TypeError):
            continue
        source_change = (request.method == 'DELETE' or '/revisions' in request.url.path
                         or isinstance(body, dict) and bool({'model_id', 'print_file_id', 'stored_path'} & body.keys()))
        marked = policies(db, kind, identifier)
        if kind == 'job' and request.method == 'DELETE':
            from sqlalchemy import text
            marked = db.execute(text('SELECT 1 FROM controlled_jobs WHERE job_id=:id'), {'id': identifier}).first()
        if source_change and marked:
            refuse(db, 'controlled_part_release_required', kind, int(identifier))


def json_content(value):
    """Match FastAPI's application/json and application/*+json body handling."""
    import email.message
    if not value:
        return True
    message = email.message.Message()
    message['content-type'] = value
    return message.get_content_maintype() == 'application' and (
        message.get_content_subtype() == 'json' or message.get_content_subtype().endswith('+json'))


def reference_id(value):
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return value
