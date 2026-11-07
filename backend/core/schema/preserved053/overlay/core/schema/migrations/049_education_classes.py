"""D2: additive classes, assignments and retained assessment records."""
import re
import uuid
from sqlalchemy import inspect, text
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:049-education-classes'
# D2 owns this migration; it has no dependency on parallel tier-value features.
DDL = {
    'education_classes': '''(
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        org_id INTEGER NOT NULL,
        cost_center_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        term TEXT NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'active',
        revision INTEGER NOT NULL DEFAULT 1,
        created_by INTEGER NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(id,org_id),
        CHECK(state IN ('active','archived')),
        CHECK(revision>0),
        FOREIGN KEY(cost_center_id,org_id) REFERENCES education_cost_centers(id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(created_by,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT
    )''',
    'education_assignments': '''(
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        org_id INTEGER NOT NULL,
        class_id VARCHAR(36) NOT NULL,
        title TEXT NOT NULL,
        brief TEXT NOT NULL,
        due_at VARCHAR(32),
        max_print_seconds INTEGER,
        max_material_grams FLOAT,
        allowed_materials TEXT NOT NULL,
        submission_quota INTEGER,
        auto_print_seconds INTEGER,
        auto_material_grams FLOAT,
        auto_printer_ids TEXT NOT NULL,
        authorized_by INTEGER NOT NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'active',
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(id,org_id),
        CHECK(state IN ('active','closed')),
        CHECK(revision>0),
        CHECK(max_print_seconds IS NULL OR max_print_seconds>0),
        CHECK(max_material_grams IS NULL OR max_material_grams>0),
        CHECK(submission_quota IS NULL OR submission_quota>0),
        CHECK(auto_print_seconds IS NULL OR auto_print_seconds>0),
        CHECK(auto_material_grams IS NULL OR auto_material_grams>0),
        FOREIGN KEY(class_id,org_id) REFERENCES education_classes(id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(authorized_by,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT
    )''',
    'education_assignment_submissions': '''(
        submission_id INTEGER PRIMARY KEY NOT NULL,
        org_id INTEGER NOT NULL,
        assignment_id VARCHAR(36) NOT NULL,
        feedback TEXT NOT NULL DEFAULT '',
        grade VARCHAR(100) NOT NULL DEFAULT '',
        learning_status VARCHAR(24) NOT NULL DEFAULT 'submitted',
        reviewed_by INTEGER,
        revision INTEGER NOT NULL DEFAULT 1,
        first_approved_at TIMESTAMP,
        attached_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(submission_id,org_id),
        CHECK(revision>0),
        CHECK(learning_status IN ('submitted','reviewed','needs_revision','complete')),
        FOREIGN KEY(submission_id,org_id) REFERENCES education_submissions(id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(assignment_id,org_id) REFERENCES education_assignments(id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(reviewed_by,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT
    )''',
    'education_assessment_revisions': '''(
        id VARCHAR(36) PRIMARY KEY NOT NULL,
        submission_id INTEGER NOT NULL,
        org_id INTEGER NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>1),
        reviewed_by INTEGER,
        old_grade VARCHAR(100) NOT NULL,
        new_grade VARCHAR(100) NOT NULL,
        old_feedback TEXT NOT NULL,
        new_feedback TEXT NOT NULL,
        old_learning_status VARCHAR(24) NOT NULL,
        new_learning_status VARCHAR(24) NOT NULL,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(submission_id,revision),
        FOREIGN KEY(submission_id,org_id) REFERENCES education_assignment_submissions(submission_id,org_id) ON DELETE RESTRICT,
        FOREIGN KEY(reviewed_by,org_id) REFERENCES education_memberships(user_id,org_id) ON DELETE RESTRICT
    )''',
}
COLUMNS = {
    'education_classes': 'id org_id cost_center_id title term state revision created_by created_at',
    'education_assignments': 'id org_id class_id title brief due_at max_print_seconds max_material_grams allowed_materials submission_quota auto_print_seconds auto_material_grams auto_printer_ids authorized_by state revision created_at',
    'education_assignment_submissions': 'submission_id org_id assignment_id feedback grade learning_status reviewed_by revision first_approved_at attached_at updated_at',
    'education_assessment_revisions': 'id submission_id org_id revision reviewed_by old_grade new_grade old_feedback new_feedback old_learning_status new_learning_status created_at',
}


HISTORY = 'education_assessment_revisions'
FUNCTION = 'odin_reject_assessment_revision_mutation'


def sqlite_triggers():
    triggers = {f'trg_assessment_revisions_no_{action.lower()}':
        f"CREATE TRIGGER trg_assessment_revisions_no_{action.lower()} BEFORE {action} ON {HISTORY} "
        "BEGIN SELECT RAISE(ABORT, 'assessment revisions are immutable'); END"
        for action in ('UPDATE', 'DELETE')}
    # REPLACE can delete without firing DELETE triggers when recursive_triggers is off.
    triggers['trg_assessment_revisions_no_replace'] = (
        f'CREATE TRIGGER trg_assessment_revisions_no_replace BEFORE INSERT ON {HISTORY} '
        f'WHEN EXISTS(SELECT 1 FROM {HISTORY} WHERE id=NEW.id OR '
        '(submission_id=NEW.submission_id AND revision=NEW.revision)) '
        "BEGIN SELECT RAISE(ABORT, 'assessment revisions are immutable'); END")
    return triggers


def pg_functions():
    return {FUNCTION: " BEGIN RAISE EXCEPTION 'assessment revisions are immutable'; END; "}


def pg_triggers():
    return {(HISTORY, f'trg_assessment_revisions_no_{action.lower()}'):
        f'CREATE TRIGGER trg_assessment_revisions_no_{action.lower()} BEFORE {action} ON public.{HISTORY} '
        f"FOR EACH {'STATEMENT' if action == 'TRUNCATE' else 'ROW'} EXECUTE FUNCTION {FUNCTION}()"
        for action in ('UPDATE', 'DELETE', 'TRUNCATE')}


def _install_guards(connection):
    if connection.dialect.name == 'sqlite':
        for ddl in sqlite_triggers().values():
            connection.exec_driver_sql(ddl.replace('CREATE TRIGGER ', 'CREATE TRIGGER IF NOT EXISTS ', 1))
    else:
        for name, body in pg_functions().items():
            connection.exec_driver_sql(f'CREATE OR REPLACE FUNCTION public.{name}() RETURNS trigger '
                'LANGUAGE plpgsql SET search_path TO pg_catalog, public AS $$' + body + '$$')
        for (table, name), ddl in pg_triggers().items():
            connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS {name} ON public.{table}')
            connection.exec_driver_sql(ddl.replace('EXECUTE FUNCTION ', 'EXECUTE FUNCTION public.'))


def _validate_guards(connection):
    if connection.dialect.name == 'sqlite':
        actual = dict(connection.execute(text("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=:t"),
                                         {'t': HISTORY}).all())
        if actual != sqlite_triggers():
            raise MigrationError('Assessment revision guards differ')
    else:
        # Match 040's native catalog validation; rendered SQL depends on search_path.
        rows = connection.execute(text(
            "SELECT t.tgname,t.tgtype,t.tgenabled,t.tgqual,t.tgnargs,p.proname,"
            "p.prosrc,p.proconfig,p.prosecdef,l.lanname,n.nspname "
            "FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid "
            "JOIN pg_namespace n ON n.oid=p.pronamespace "
            "JOIN pg_language l ON l.oid=p.prolang "
            "WHERE t.tgrelid=to_regclass(:t) AND NOT t.tgisinternal ORDER BY t.tgname"),
            {'t': HISTORY}).all()
        expected = sorted((f'trg_assessment_revisions_no_{action}', kind, 'O', None, 0,
            FUNCTION, pg_functions()[FUNCTION], ['search_path=pg_catalog, public'], False, 'plpgsql', 'public')
            for action, kind in (('update', 19), ('delete', 11), ('truncate', 34)))
        if [tuple(row) for row in rows] != expected:
            raise MigrationError('Assessment revision guards differ')


def _shape(connection, table, schema=None):
    i = inspect(connection)
    def normalized(value):
        # Formatting/casts may differ between dialects; quoted values must not.
        parts = re.split(r"('(?:''|[^'])*')", str(value))
        return ''.join(part if index % 2 else re.sub(
            r"::(?:text|character varying)|[\s()]", '', part).lower()
            for index, part in enumerate(parts))
    return (
        [(c['name'], str(c['type']), c['nullable'], normalized(c.get('default')))
         for c in i.get_columns(table, schema=schema)],
        i.get_pk_constraint(table, schema=schema)['constrained_columns'],
        sorted(tuple(c['column_names']) for c in i.get_unique_constraints(table, schema=schema)),
        sorted((tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']),
                f['options'].get('ondelete')) for f in i.get_foreign_keys(table, schema=schema)),
        sorted(normalized(c['sqltext']) for c in i.get_check_constraints(table, schema=schema)),
        sorted((tuple(x['column_names']), x['unique']) for x in i.get_indexes(table, schema=schema)
               if not x.get('duplicates_constraint')),
    )


def validate(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported classes dialect')
    for table, ddl in DDL.items():
        name = '_classes_contract_' + uuid.uuid4().hex[:12]
        pg = connection.dialect.name == 'postgresql'
        # As in039, PostgreSQL temp tables cannot reference permanent tables.
        reference = r',\s*FOREIGN KEY\(([^)]+)\) REFERENCES (\w+)\(([^)]+)\) ON DELETE RESTRICT'
        expected_ddl = re.sub(reference, '', ddl) if pg else ddl
        connection.exec_driver_sql(('CREATE TEMPORARY TABLE ' if pg else 'CREATE TABLE ') + name + expected_ddl)
        try:
            schema = connection.execute(text('SELECT nspname FROM pg_namespace WHERE oid=pg_my_temp_schema()')).scalar_one() if pg else None
            wanted = list(_shape(connection, name, schema))
            if pg:
                wanted[3] = sorted((tuple(c.strip() for c in columns.split(',')), target,
                                    tuple(c.strip() for c in keys.split(',')), 'RESTRICT')
                                   for columns, target, keys in re.findall(reference, ddl))
            if _shape(connection, table) != tuple(wanted):
                raise MigrationError('Classes schema differs: ' + table)
            if pg and connection.execute(text('SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass(:t) AND NOT convalidated'), {'t': table}).first():
                raise MigrationError('Classes constraints are not validated')
        finally:
            connection.exec_driver_sql('DROP TABLE ' + name)
    _validate_guards(connection)


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported classes dialect')
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    for table, ddl in DDL.items():
        if not inspect(connection).has_table(table):
            connection.exec_driver_sql('CREATE TABLE ' + table + ddl)
    _install_guards(connection)
    validate(connection)
