"""Differential and scale checks against the released publication contracts."""
import importlib
import subprocess
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter

import pytest
from sqlalchemy import event, text

from core.schema import bootstrap_database
from core.schema import timelapse_publication_bootstrap_validation as fast
from core.schema.migrator import MigrationError
from core.timelapse_publication_schema import facts, publications
from tests.test_contracts.test_education_classes_migration import native_engine, pg_engine
from tests.test_contracts.test_timelapse_publication_migration import owned_frame_publication
from tests.test_contracts.test_timelapse_publication_retained import (
    completed_trim_retirement, phase_fixture,
    test_admitted_nonframe_operation_has_exact_result_lifetime_and_manifest as add_edit,
)

P = importlib.import_module('core.schema.migrations.019_timelapse_publications')
M = importlib.import_module('core.schema.migrations.023_timelapse_retirement_membership')
BOTH = ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgresql)]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def old_walk():
    filename = 'backend/core/timelapse_publication_validation.py'
    source = subprocess.check_output(['git', 'show', '09a04a3f:' + filename], cwd=ROOT)
    namespace = {}
    exec(compile(source, filename, 'exec'), namespace)
    return namespace['validate_retained']


def seed(engine):
    # Reuse the established cross-table ownership and frame fixture.
    owned_frame_publication.__wrapped__(engine)


def generate_frames(db, count):
    """Copy the complete fixture with distinct frame identities and manifests."""
    sequence = """WITH RECURSIVE seq(n) AS (
        SELECT 2 UNION ALL SELECT n+1 FROM seq WHERE n < :count
    ) """
    padded = "printf('%06d', n)" if db.dialect.name == 'sqlite' else "lpad(CAST(n AS text), 6, '0')"
    op_id = "'frame-' || CAST(n AS text)"
    for table, replacements in [
        (publications, {'operation_id': op_id, 'private_directory': "'publish_frame-' || CAST(n AS text)",
                        'frame_number': 'n', 'destination_path': "'frames_namespace/frame_' || " + padded + " || '.jpg'"}),
        (facts, {'operation_id': op_id, 'relative_path': "CASE WHEN role='private_root' THEN 'publish_frame-' || CAST(n AS text) ELSE relative_path END"}),
    ]:
        columns = list(table.columns.keys())
        values = [replacements.get(key, key) for key in columns]
        db.execute(text(sequence + f"INSERT INTO {table.name} ({', '.join(columns)}) SELECT {', '.join(values)} "
                        f"FROM seq CROSS JOIN {table.name} WHERE operation_id='frame-op'"), {'count': count})
    db.execute(text('UPDATE timelapse_frame_bindings SET next_number=:n'), {'n': count + 1})


@contextmanager
def statements(engine):
    count = [0]

    def observe(*args):
        count[0] += 1

    event.listen(engine, 'before_cursor_execute', observe)
    try:
        yield count
    finally:
        event.remove(engine, 'before_cursor_execute', observe)


def verdict(check, db):
    try:
        check(db)
    except MigrationError as error:
        return str(error)
    return None


def equivalent(db, old_walk):
    assert verdict(fast.validate_retained, db) == verdict(old_walk, db)
    for module, check in ((P, fast.validate_publications), (M, fast.validate_membership)):
        assert verdict(lambda c: check(module, c), db) == verdict(module.validate, db)


@pytest.mark.parametrize('native_engine', BOTH, indirect=True)
def test_publication_history_scale(native_engine):
    engine = native_engine
    bootstrap_database(engine)
    seed(engine)
    phase_fixture(engine, 'complete')
    # Settle the synthetic historical job ID before measuring a repeat boot.
    assert bootstrap_database(engine)['applied'] == []
    # The CI budget is SQL statements. Timings are observations on the host.
    sizes = (1000, 120000) if engine.dialect.name == 'sqlite' else (10, 100)
    budgets = []
    for size in sizes:
        with engine.begin() as db:
            with db.begin_nested() as history:
                generate_frames(db, size)
                assert db.exec_driver_sql('SELECT count(*) FROM timelapse_publications').scalar_one() == size
                assert db.exec_driver_sql('SELECT count(*) FROM timelapse_publication_facts').scalar_one() == 6 * size
                with statements(engine) as count:
                    start = perf_counter()
                    fast.validate_retained(db)
                    elapsed = perf_counter() - start
                budgets.append(count[0])
                if size == sizes[0]:
                    from core.timelapse_publication_validation import validate_retained
                    with statements(engine) as old:
                        validate_retained(db)
                    print(f'pinned retained walk: {old[0]} statements at {size} operations')
                seed_sealed_retirement(db)
                start = perf_counter()
                M.validate_retained(db)
                print(f'{size} operations: {budgets[-1]} statements, retained {elapsed:.3f}s, membership {perf_counter()-start:.3f}s')
                if size == sizes[0]:
                    history.rollback()
        if size == sizes[-1]:
            start = perf_counter()
            assert bootstrap_database(engine)['applied'] == []
            print(f'repeat bootstrap: {perf_counter()-start:.3f}s')
    assert budgets[0] == budgets[1] <= 30


def another_capture(db):
    """A second valid lifetime and bound namespace in the same history."""
    from core.timelapse_ownership_schema import anchors
    from core.timelapse_publication_schema import lifetimes, versions, namespaces, bindings

    for table, key, original, changes in (
        (anchors, 'operation_id', 'owned', dict(operation_id='other', timelapse_id=399)),
        (lifetimes, 'lifetime_nonce', 'owned', dict(lifetime_nonce='other', original_timelapse_id=399)),
        (versions, 'version_nonce', 'owned', dict(version_nonce='other', lifetime_nonce='other')),
        (namespaces, 'namespace_id', 'namespace', dict(namespace_id='other', lifetime_nonce='other', relative_path='frames_other')),
        (bindings, 'lifetime_nonce', 'owned', dict(lifetime_nonce='other', namespace_id='other')),
    ):
        row = db.execute(table.select().where(table.c[key] == original)).mappings().one()
        db.execute(table.insert(), dict(row, **changes))



def seed_sealed_retirement(db):
    # A closed 023 membership with a real member, admitted by its own triggers.
    another_capture(db)
    db.exec_driver_sql("DELETE FROM timelapse_frame_bindings WHERE lifetime_nonce='other'")
    db.exec_driver_sql("UPDATE timelapse_frame_namespaces SET phase='planned',device=NULL,inode=NULL,"
                       "uid=NULL,gid=NULL,mode=NULL WHERE namespace_id='other'")
    db.exec_driver_sql("INSERT INTO printers(id,name) VALUES(9,'Test printer')")
    db.exec_driver_sql("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status,ownership_operation_id,retirement_operation_id)"
                       " VALUES(399,9,444,'owned-original.mp4','deleting','other','other')")
    db.exec_driver_sql("INSERT INTO timelapse_retirements(operation_id,timelapse_id,printer_id,owner_state,owner_org_id,"
                       "filename,root_device,root_inode,state,revision) VALUES('other',399,9,'known',1,'owned-original.mp4',1,100,'pending',1)")
    db.exec_driver_sql("INSERT INTO timelapse_retirement_objects(operation_id,object_index,relative_path,kind,state)"
                       " VALUES('other',0,'frames_other','directory','originally_absent')")
    db.exec_driver_sql("INSERT INTO timelapse_retirement_lifetime_bindings(operation_id,version_nonce,lifetime_nonce,phase,"
                       "object_count,created_at) VALUES('other','other','other','building',1,CURRENT_TIMESTAMP)")
    db.exec_driver_sql("INSERT INTO timelapse_retirement_members(operation_id,object_index,role,namespace_id)"
                       " VALUES('other',0,'frame_namespace','other')")
    db.exec_driver_sql("UPDATE timelapse_retirement_lifetime_bindings SET phase='sealed',sealed_at=CURRENT_TIMESTAMP WHERE operation_id='other'")
    db.exec_driver_sql("UPDATE timelapse_retirements SET state='complete',completed_at=CURRENT_TIMESTAMP WHERE operation_id='other'")
    db.exec_driver_sql("DELETE FROM timelapses WHERE id=399")


def history(engine, kind):
    # Create the migration-era baseline, including the live columns used by the
    # 023 membership guards. No metadata from a current customer is needed.
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE timelapses(id INTEGER PRIMARY KEY,printer_id INTEGER,print_job_id INTEGER,'
                           'filename TEXT,status TEXT,frame_count INTEGER,completed_at TIMESTAMP,duration_seconds INTEGER,file_size_mb INTEGER)')
        db.exec_driver_sql('CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,action TEXT,entity_type TEXT,entity_id INTEGER,details TEXT)')
        for name in ('015_timelapse_retirements', '016_timelapse_ownership', '017_timelapse_recovery', '018_timelapse_unlinks'):
            importlib.import_module('core.schema.migrations.' + name).apply(db)
    seed(engine)
    if kind in ('trim', 'speed', 'encode'):
        add_edit(engine, kind)
    elif kind in ('retired', 'retained'):
        completed_trim_retirement(engine)
        if kind == 'retained':
            with engine.begin() as db:
                db.exec_driver_sql("UPDATE timelapse_retained_versions SET state='retained', retire_operation_id=NULL")
    else:
        phase_fixture(engine, kind)
    with engine.begin() as db:
        another_capture(db)
        M.apply(db)


# Every mutation starts from the indicated independently valid history. The
# expected line is the actual refuse() call in the immutable reference source.
# Several malformed values are rejected even earlier by the native schema.
FRAME = 'timelapse_publications'
FACT = 'timelapse_publication_facts'
OWNER = 'timelapse_ownership_anchors'
ROOT_TABLE = 'timelapse_media_lifetimes'
SPACE = 'timelapse_frame_namespaces'
VERSION = 'timelapse_media_versions'
BIND = 'timelapse_frame_bindings'
REF = 'timelapse_retained_versions'


def change(table, assignment, predicate):
    return f"UPDATE {table} SET {assignment} WHERE {predicate}"


def frame(assignment):
    return change(FRAME, assignment, "kind='frame'")


def fact(role, assignment, op='frame-op'):
    return change(FACT, assignment, f"role='{role}' AND operation_id='{op}'")


def remove(role, op='frame-op'):
    return f"DELETE FROM {FACT} WHERE role='{role}' AND operation_id='{op}'"


DAMAGES = {
    'complete': [
        (23, change(BIND, 'next_number=0', "lifetime_nonce='owned'")),
        (27, frame("payload_digest_sha256='bad'")),
        (32, f"DELETE FROM {VERSION} WHERE version_nonce='other'"),
        (38, change(OWNER, "owner_state='unresolved',owner_org_id=NULL,ownership_source='capture_unresolved'", "operation_id='owned'")),
        (50, change(VERSION, "lifetime_nonce='other'", "version_nonce='owned'")),
        (51, change(ROOT_TABLE, 'original_printer_id=2', "lifetime_nonce='owned'")),
        (53, change(ROOT_TABLE, 'root_mode=33152', "lifetime_nonce='owned'")),
        (70, fact('root', 'mode=33152')),
        (71, fact('root', 'uid=502')),
        (72, fact('root', 'byte_count=1')),
        (76, fact('output', "kind='other'")),
        (78, fact('output', 'link_count=2')),
        (79, fact('output', 'gid=1')),
        (85, change(SPACE, "relative_path='legacy'", "namespace_id='namespace'")),
        (88, change(SPACE, 'uid=NULL', "namespace_id='namespace'")),
        (92, change(SPACE, 'inode=100', "namespace_id='namespace'")),
        (93, change(SPACE, 'device=NULL,inode=NULL,uid=NULL,gid=NULL,mode=NULL', "namespace_id='namespace'")),
        (94, change(SPACE, "phase='planned'", "namespace_id='namespace'")),
        (101, change(SPACE, "phase='blocked'", "namespace_id='namespace'")),
        (117, frame("source_lifetime='other'")),
        (118, frame("source_filename='other.mp4'")),
        (119, frame('result_tid=400')),
        (121, frame("private_directory='wrong'")),
        (124, frame("result_nonce='other',result_lifetime='other',result_tid=399")),
        (127, change(BIND, 'next_number=1', "lifetime_nonce='owned'")),
        (129, frame("destination_path='frames_other/frame_000001.jpg'")),
        (141, f'DELETE FROM {FACT}'),
        (144, fact('root', "role='unexpected'")),
        (153, fact('output_parent', 'inode=999')),
        (156, remove('root')),
        (159, remove('source_parent')),
        (182, remove('output_parent')),
        (188, frame("phase='admitted',published_ctime_ns=NULL,state='active',completed_at=NULL")),
        (190, remove('private_root')),
        (190, remove('private_creation')),
        (191, fact('private_root', 'inode=100')),
        (194, frame("phase='private_created',published_ctime_ns=NULL,state='active',completed_at=NULL")),
        (196, remove('output')),
        (196, fact('output', 'inode=222')),
        (196, fact('output', "relative_path='unrelated.jpg'")),
        (197, fact('output', 'byte_count=0')),
        (198, fact('output', "digest_sha256='" + 'b'*64 + "'")),
        (185, fact('output_parent', "kind='file', mode=33152, link_count=1, byte_count=4, mtime_ns=1, ctime_ns=1, digest_sha256='" + 'a'*64 + "'")),
        (71, change(SPACE, 'uid=502', "namespace_id='namespace'")),
        (233, frame("destination_path='../outside.jpg'")),
    ],
    'trim': [
        (60, change(OWNER, 'scheduled_job_id=555', "operation_id='trim-result'")),
        (63, f"DELETE FROM {FRAME} WHERE kind='trim'"),
        (131, change(FRAME, "destination_path='wrong.mp4'", "kind='trim'")),
        (161, remove('source', 'trim-op')),
        (184, fact('output_parent', "kind='file', mode=33152, link_count=1, byte_count=4, mtime_ns=1, ctime_ns=1, digest_sha256='" + 'a'*64 + "'", 'trim-op')),
        (165, fact('source', "relative_path='wrong.mp4'", 'trim-op')),
        (180, fact('source', 'byte_count=34359738369', 'trim-op')),
        *((233, change(FRAME, assignment, "kind='trim'")) for assignment in
          ("trim_start='1e-6'", "trim_start='0.0'", "trim_start='-0'", "trim_end='9223372036854.775808'", "trim_end='0'")),
    ],
    'speed': [
        (120, change(ROOT_TABLE, 'root_inode=888', "lifetime_nonce='speed-result'")),
        (136, change(FRAME, "kind='trim',speed_numerator=NULL,speed_denominator=NULL,trim_start='0',trim_end='1'", "kind='speed'")),
    ],
    'encode': [
        (168, fact('source', "relative_path='unbound.jpg'", 'encode-op')),
        (172, fact('source', "relative_path='frames_namespace/frame_01.jpg'", 'encode-op')),
        (174, fact('source', 'byte_count=7', 'encode-op')),
        (176, fact('source', 'ctime_ns=299', 'encode-op')),
        (178, remove('source_parent', 'encode-op')),
    ],
    'retired': [
        (138, change(FRAME, "kind='speed',trim_start=NULL,trim_end=NULL,speed_numerator=1,speed_denominator=1", "kind='trim'")),
        (192, fact('private_creation', 'inode=200', 'trim-op')),
        (202, change(REF, "filename='wrong.mp4'", "version_nonce='owned'")),
        (219, f"INSERT INTO {REF}(version_nonce,lifetime_nonce,retained_by_operation,filename,state) VALUES('other','other','frame-op','owned-original.mp4','retained')"),
        (230, 'DELETE FROM timelapse_retirement_unlinks'),
        (230, 'DELETE FROM timelapse_retirement_moves'),
        (230, "UPDATE timelapse_retirement_moves SET digest_sha256='" + 'd'*64 + "'"),
        (230, 'UPDATE timelapse_retirement_objects SET inode=inode+1'),
        (230, 'UPDATE timelapse_retirement_objects SET mtime_ns=mtime_ns+1'),
        (231, 'UPDATE timelapse_retirements SET root_inode=888'),
    ],
}


def damage(db, sql):
    """Exercise the direct walk even if a native constraint normally fires first."""
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy import inspect
    # A case can violate multiple constraints. Preserve all that allow the case.
    while True:
        try:
            with db.begin_nested():
                db.exec_driver_sql(sql)
            return
        except IntegrityError as error:
            if db.dialect.name == 'sqlite':
                if 'CHECK constraint failed' not in str(error.orig):
                    raise
                db.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
                db.exec_driver_sql(sql)
                db.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')
                return
            info = error.orig.diag
            if error.orig.sqlstate not in ('23503', '23514'):
                raise
            quote = db.dialect.identifier_preparer.quote_identifier
            assert info.table_name in inspect(db).get_table_names()
            db.exec_driver_sql(f'ALTER TABLE {quote(info.table_name)} DROP CONSTRAINT {quote(info.constraint_name)}')


@pytest.mark.parametrize('native_engine', BOTH, indirect=True)
@pytest.mark.parametrize('kind', ['admitted', 'private_created', 'output_ready', 'publish_pending', 'published',
                                  'complete', 'trim', 'speed', 'encode', 'retained', 'retired'])
def test_generated_histories_and_damaged_records(native_engine, old_walk, kind):
    history(native_engine, kind)
    with native_engine.begin() as db:
        equivalent(db, old_walk)
        assert verdict(M.validate, db) is None
        cases = list(DAMAGES.get(kind, ()))
        if kind == 'complete' and db.dialect.name == 'sqlite':
            cases.append((23, change(BIND, 'next_number=1.5', "lifetime_nonce='owned'")))
        for line, sql in cases:
            with db.begin_nested() as saved:
                damage(db, sql)
                with pytest.raises(MigrationError) as reference:
                    old_walk(db)
                assert reference.traceback[-2].lineno + 1 == line, sql
                equivalent(db, old_walk)
                saved.rollback()


@pytest.mark.parametrize('native_engine', BOTH, indirect=True)
def test_duplicate_authority_without_unique_constraints(native_engine, old_walk):
    history(native_engine, 'speed')
    with native_engine.begin() as db:
        for table, where, line in (
            (publications, "kind='speed'", 134), (facts, "operation_id='speed-op' AND role='source'", 163)
        ):
            with db.begin_nested() as saved:
                # Simulate an adopted table missing its native unique keys. Both
                # copies of the manifest path / result are individually valid.
                db.exec_driver_sql(f'ALTER TABLE {table.name} RENAME TO damaged_original')
                db.exec_driver_sql(f'CREATE TABLE {table.name} AS SELECT * FROM damaged_original')
                keys = list(table.columns.keys())
                overrides = {'operation_id': "'extra-speed'", 'private_directory': "'publish_extra-speed'"} if table is publications else {}
                projection = ', '.join(overrides.get(k, k) for k in keys)
                db.exec_driver_sql(f'INSERT INTO {table.name} SELECT {projection} FROM damaged_original WHERE {where}')
                with pytest.raises(MigrationError) as reference:
                    old_walk(db)
                assert reference.traceback[-2].lineno + 1 == line
                equivalent(db, old_walk)
                saved.rollback()


@pytest.mark.parametrize('native_engine', BOTH, indirect=True)
def test_schema_and_trigger_verdicts(native_engine, old_walk):
    history(native_engine, 'complete')
    with native_engine.begin() as db:
        # Drop leaf tables so the verdict is the missing-table check itself.
        missing = [("DROP TABLE " + M.TABLES[-1].name, 'Membership table missing'),
                   ("DROP TABLE " + P.TABLES[-1].name, 'Publication table missing')]
        if db.dialect.name == 'sqlite':
            name = next(iter(M.sqlite_triggers()))
            schema_cases = [
                ([f'DROP TRIGGER {name}'], 'Membership trigger missing'),
                ([f'DROP TRIGGER {name}', f'CREATE TRIGGER {name} BEFORE INSERT ON {M.TABLES[0].name} BEGIN SELECT 1; END'],
                 'Membership trigger differs'),
            ]
        else:
            name = next(iter(M.pg_functions()))
            schema_cases = [
                ([f'ALTER FUNCTION public.{name}() SECURITY DEFINER'], 'Membership function differs'),
                ([f'ALTER TABLE public.{M.TABLES[0].name} DISABLE TRIGGER {name}'], 'Membership native trigger differs'),
            ]
        for sql, message in missing:
            if db.dialect.name == 'postgresql':
                sql += ' CASCADE'
            schema_cases.append(([sql], message))
        for statements_, message in schema_cases:
            with db.begin_nested() as saved:
                for sql in statements_:
                    db.exec_driver_sql(sql)
                equivalent(db, old_walk)
                assert verdict(M.validate, db) == message
                if message == 'Publication table missing':
                    with pytest.raises(MigrationError) as reference:
                        old_walk(db)
                    assert reference.traceback[-2].lineno + 1 == 209
                saved.rollback()


@pytest.mark.parametrize('native_engine', BOTH, indirect=True)
@pytest.mark.parametrize('filename,expected', [
    ('timelapse_publication_validation.py', 'Publication source contract changed'),
    ('timelapse_recipes.py', 'Publication recipe source contract changed'),
    ('timelapse_retirement_membership_validation.py', 'Membership source differs'),
])
def test_source_change_reaches_original_refusal(native_engine, filename, expected, monkeypatch):
    history(native_engine, 'admitted')
    read_bytes = Path.read_bytes

    def changed(path):
        original = read_bytes(path)
        return original + b'\n' if path.name == filename else original

    with native_engine.begin() as db:
        fast.validate_publications(P, db)
        proof = db.get_transaction()
        monkeypatch.setattr(Path, 'read_bytes', changed)
        for module, check in ((P, fast.validate_publications), (M, fast.validate_membership)):
            assert verdict(lambda c: check(module, c), db) == verdict(module.validate, db)
        assert verdict(lambda c: fast.validate_membership(M, c), db) == expected
        assert verdict(lambda c: fast.validate_membership(M, c, proof), db) == expected


@pytest.mark.parametrize('native_engine', BOTH, indirect=True)
def test_reuse_stops_at_bootstrap_and_first_apply(native_engine, monkeypatch):
    from core.schema.migrator import apply_python_migration
    original = fast.validate_retained
    walks = []

    def counted(db):
        walks.append(db.get_transaction())
        original(db)

    monkeypatch.setattr(fast, 'validate_retained', counted)
    initial = bootstrap_database(native_engine)['applied']
    assert P.MIGRATION_ID in initial and M.MIGRATION_ID in initial
    assert len(walks) == 2
    walks.clear()
    assert bootstrap_database(native_engine)['applied'] == []
    per_run = 1 if native_engine.dialect.name == 'sqlite' else 2
    assert len(walks) == per_run
    assert bootstrap_database(native_engine)['applied'] == []
    assert len(walks) == 2 * per_run and walks[0] is not walks[-1]
    with native_engine.begin() as db:
        apply_python_migration(db, M.__name__)
        assert len(walks) == 2 * per_run + 1
        # Outside the closed bootstrap sequence even the same transaction must
        # recheck history after callers have had an opportunity to write.
        apply_python_migration(db, P.__name__)
        apply_python_migration(db, M.__name__)
        assert len(walks) == 2 * per_run + 3
        db.execute(text("DELETE FROM odin_schema_migrations WHERE migration_id='python:020-ams-evidence'"))
    assert bootstrap_database(native_engine)['applied'] == ['python:020-ams-evidence']
    assert len(walks) == 2 * per_run + 5


@pytest.mark.parametrize('native_engine', BOTH, indirect=True)
def test_nested_destination_requires_each_parent(native_engine, old_walk):
    history(native_engine, 'trim')
    with native_engine.begin() as db:
        destination = "'folder/nested/trim.mp4'"
        db.exec_driver_sql(change(OWNER, 'filename=' + destination, "operation_id='trim-result'"))
        db.exec_driver_sql(change(FRAME, 'destination_path=' + destination, "kind='trim'"))
        db.exec_driver_sql(fact('output_parent', "relative_path='folder/nested',inode=901", 'trim-op'))
        equivalent(db, old_walk)
        assert verdict(old_walk, db) == 'Publication retained authority differs'
        from tests.test_contracts.test_timelapse_publication_retained import DIRECTORY
        db.execute(facts.insert(), dict(DIRECTORY, operation_id='trim-op', role='output_parent', relative_path='folder', inode=900))
        equivalent(db, old_walk)
        assert verdict(M.validate, db) is None


def test_adopted_case_insensitive_keys_preserve_requested_nonce(old_walk):
    from sqlalchemy import create_engine

    engine = create_engine('sqlite://')

    @event.listens_for(engine, 'before_cursor_execute', retval=True)
    def adopted_collation(conn, cursor, sql, params, context, many):
        for table, key in ((OWNER, 'operation_id'), (VERSION, 'version_nonce'), (FRAME, 'result_nonce')):
            if f'CREATE TABLE {table} (' in sql:
                sql = sql.replace(key + ' VARCHAR(64) NOT NULL', key + ' VARCHAR(64) COLLATE NOCASE NOT NULL')
        return sql, params

    try:
        history(engine, 'trim')
        with engine.begin() as db:
            equivalent(db, old_walk)
            assert verdict(M.validate, db) is None
            db.exec_driver_sql(change(FRAME, "result_nonce='TRIM-RESULT'", "kind='trim'"))
            equivalent(db, old_walk)
            assert verdict(M.validate, db) is None
            db.exec_driver_sql(frame("source_nonce='OWNED',result_nonce='OWNED'"))
            equivalent(db, old_walk)
            assert verdict(M.validate, db) == 'Publication retained authority differs'
    finally:
        engine.dispose()


def test_cold_source_check_precedes_loading_retained_code():
    import sys

    # An unreadable Python module must still produce the historical pin error.
    # Start without the modules cached by the fixtures in this pytest process.
    script = """
from importlib import import_module
from pathlib import Path
import sys
from unittest.mock import patch
publication = import_module('core.schema.migrations.019_timelapse_publications')
membership = import_module('core.schema.migrations.023_timelapse_retirement_membership')
from core.schema.migrator import MigrationError
from core.schema.timelapse_publication_bootstrap_validation import validate_membership, validate_publications
assert 'core.timelapse_publication_validation' not in sys.modules
assert 'core.timelapse_recipes' not in sys.modules
read = Path.read_bytes
for module, check, damaged, expected in (
    (publication, validate_publications, 'timelapse_publication_validation.py', 'Publication source contract changed'),
    (membership, validate_membership, 'timelapse_retirement_membership_validation.py', 'Membership source differs'),
):
    with patch.object(Path, 'read_bytes', lambda p: b'invalid Python!' if p.name == damaged else read(p)):
        try:
            check(module, None)
        except MigrationError as error:
            assert str(error) == expected
        else:
            raise AssertionError('Changed source was accepted')
assert 'core.schema.timelapse_publication_bootstrap_retained' not in sys.modules
"""
    subprocess.run([sys.executable, '-B', '-c', script], cwd=ROOT / 'backend', check=True)
