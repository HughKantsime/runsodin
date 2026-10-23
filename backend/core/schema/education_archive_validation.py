"""Validate the immutable 002 contract with the precise additive 036 extension.

Use a private function namespace; never patch the migration module or its checksum.
All original constraints and audit trigger validation still execute unchanged.
"""
from types import FunctionType
from sqlalchemy import inspect


def validate_education_with_archive(module, connection):
    columns = {c['name'] for c in inspect(connection).get_columns('education_submissions')}
    additions = {'archived_at', 'archived_by', 'archive_reason'}
    if not columns & additions:
        return module.validate(connection)
    if not additions <= columns:
        raise RuntimeError('Education archive metadata is incomplete')
    retained = inspect(connection).has_table('education_memberships')
    original_expected = module._expected_manifest

    def extended_expected():
        manifest = original_expected()
        shape = manifest['education_submissions']
        shape['columns'] += (('archived_at', 'datetime', True, None),
                             ('archived_by', 'integer', True, None),
                             ('archive_reason', 'text', True, None))
        shape['foreign_keys'] = tuple(sorted(shape['foreign_keys'] + (
            (('archived_by',), 'users', ('id',), 'RESTRICT'),)))
        if retained:
            for item in manifest.values():
                item['foreign_keys'] = tuple(sorted((cols, 'education_memberships', ('user_id', 'org_id'), delete) if table == 'users' and refs == ('id', 'group_id') else (cols, table, refs, delete) for cols, table, refs, delete in item['foreign_keys']))
        return manifest

    original_manifest = module._schema_manifest

    def extended_manifest(conn, tables):
        manifest = original_manifest(conn, tables)
        if conn.dialect.name == 'sqlite' and 'education_submissions' in manifest:
            # SQLite reflection can misparse an inline FK appended by ALTER.
            # PRAGMA reports the actual enforced delete action.
            keys = conn.exec_driver_sql('PRAGMA foreign_key_list(education_submissions)').fetchall()
            action = next((r[6] for r in keys if r[3] == 'archived_by' and r[2] == 'users' and r[4] == 'id'), None)
            if action != 'RESTRICT':
                raise RuntimeError('Education archive actor foreign key differs')
            shape = manifest['education_submissions']
            shape['foreign_keys'] = tuple((cols, table, refs, action if cols == ('archived_by',) else delete)
                for cols, table, refs, delete in shape['foreign_keys'])
        return manifest

    namespace = dict(vars(module))
    if retained:
        namespace['EXPECTED_FOREIGN_KEYS'] = {table: tuple((cols, 'education_memberships', ('user_id', 'org_id')) if target == 'users' and refs == ('id', 'group_id') else (cols, target, refs) for cols, target, refs in keys) for table, keys in module.EXPECTED_FOREIGN_KEYS.items()}
    namespace['_schema_manifest'] = extended_manifest
    namespace['_expected_manifest'] = extended_expected
    exact = module._validate_exact_manifest
    namespace['_validate_exact_manifest'] = FunctionType(exact.__code__, namespace, exact.__name__)
    validation = FunctionType(module.validate.__code__, namespace, module.validate.__name__)
    return validation(connection)
