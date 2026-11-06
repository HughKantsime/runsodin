"""Durable SQLite job allocation and unmarked deletion/list contracts."""
import ast
import importlib
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from core.job_identity import allocate_sqlite_job_id, insert_job
from core.schema import bootstrap_database
from tests.test_contracts.test_controlled_parts import database
from tests.test_contracts.test_controlled_round8 import new_job


@pytest.mark.parametrize('deletion', ['orm', 'bulk', 'raw'])
def test_deleted_maximum_is_never_reissued_after_restart(tmp_path, deletion):
    from modules.jobs.models import Job
    url = 'sqlite:///' + str(tmp_path / 'identities.db')
    engine = create_engine(url)
    bootstrap_database(engine)
    with Session(engine) as db:
        ids = [new_job(db) for _ in range(3)]
        db.commit()
        if deletion == 'orm':
            db.delete(db.get(Job, ids[-1]))
        elif deletion == 'bulk':
            db.query(Job).delete(synchronize_session=False)
        else:
            db.execute(text('DELETE FROM jobs'))
        db.commit()
    engine.dispose()
    engine = create_engine(url)
    bootstrap_database(engine)
    with Session(engine) as db:
        assert new_job(db) == max(ids) + 1
        db.commit()
    engine.dispose()


@pytest.mark.parametrize('table,column,extra', [
    ('jobs', 'id', ''), ('print_jobs', 'scheduled_job_id', ''),
    ('timelapses', 'print_job_id', ''), ('print_archives', 'job_id', ''),
    ('print_files', 'job_id', ''), ('education_submissions', 'job_id', ''),
    ('timelapse_ownership_anchors', 'scheduled_job_id', ''),
    ('printer_dispatch_claims', 'job_id', ''), ('dispatch_recovery_history', 'job_id', ''),
    ('job_timeline_events', 'job_id', ''), ('power_target_operations', 'scheduled_job_id', ''),
    ('audit_logs', 'entity_id', ",entity_type TEXT DEFAULT 'job'"),
    ('controlled_retention_clocks', 'resource_id', ",resource_type TEXT DEFAULT 'jobs'"),
    ('cost_identities', 'resource_id', ",resource_kind TEXT DEFAULT 'job'"),
    ('future_reference', 'parent', ',FOREIGN KEY(parent) REFERENCES jobs(id)'),
])
def test_upgrade_reserves_preexisting_referenced_ids(table, column, extra):
    migration = importlib.import_module('core.schema.migrations.055_controlled_history')
    engine = create_engine('sqlite://')
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE jobs(id INTEGER PRIMARY KEY)')
        db.exec_driver_sql('CREATE TABLE job_id_high_water(singleton INTEGER PRIMARY KEY,high_water INTEGER NOT NULL)')
        if table != 'jobs':
            db.exec_driver_sql(f'CREATE TABLE {table}({column} INTEGER{extra})')
        db.exec_driver_sql(f'INSERT INTO {table}({column}) VALUES(900)')
        migration.initialize_job_ids(db)
        # Even after all legacy references disappear, allocation cannot go back.
        db.exec_driver_sql(f'DELETE FROM {table}')
        assert allocate_sqlite_job_id(db.exec_driver_sql) == 901
    engine.dispose()


def test_delete_bumps_high_water_even_for_preupgrade_row():
    migration = importlib.import_module('core.schema.migrations.055_controlled_history')
    engine = create_engine('sqlite://')
    with engine.begin() as db:
        db.exec_driver_sql('CREATE TABLE jobs(id INTEGER PRIMARY KEY)')
        db.exec_driver_sql('CREATE TABLE job_id_high_water(singleton INTEGER PRIMARY KEY,high_water INTEGER NOT NULL)')
        db.exec_driver_sql('INSERT INTO jobs VALUES(500)')
        migration.initialize_job_ids(db)
        db.exec_driver_sql('DROP TRIGGER jobs_high_water_prevent_lower')
        db.exec_driver_sql('UPDATE job_id_high_water SET high_water=1')
        db.exec_driver_sql('DELETE FROM jobs')
        assert allocate_sqlite_job_id(db.exec_driver_sql) == 501
    engine.dispose()


def test_orm_and_raw_allocators_serialize_writers(tmp_path):
    url = 'sqlite:///' + str(tmp_path / 'writers.db')
    engine = create_engine(url, connect_args={'timeout': 15})
    bootstrap_database(engine)
    with Session(engine) as db:
        for _ in range(5):
            new_job(db)
        db.execute(text('DELETE FROM jobs'))
        db.commit()
    def create(index):
        if index % 2:
            with Session(engine) as db:
                identifier = new_job(db)
                db.commit()
                return identifier
        connection = engine.raw_connection()
        try:
            cursor = connection.cursor()
            insert_job(cursor, 'INSERT INTO jobs (item_name,status) VALUES (?, ?)', ('raw', 'pending'))
            identifier = cursor.lastrowid
            connection.commit()
            return identifier
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(create, range(12)))
    assert sorted(ids) == list(range(6, 18))
    engine.dispose()


def test_unmarked_deletion_creates_no_controlled_audits_or_tombstones(database):
    from core.controlled_deletion import redact_deleted_job_history
    before = database.execute(text('SELECT count(*) FROM audit_logs')).scalar_one()
    redact_deleted_job_history(database, 1)
    database.execute(text('DELETE FROM jobs WHERE id=1'))
    database.commit()
    assert database.execute(text('SELECT count(*) FROM audit_logs')).scalar_one() == before
    assert database.execute(text('SELECT count(*) FROM controlled_job_tombstones')).scalar_one() == 0


def test_unmarked_print_job_lists_have_constant_statement_count(database):
    from core.controlled_parts import policies
    database.info['_controlled_request'] = True
    queries = []
    def record(conn, cursor, statement, params, context, many):
        queries.append(statement)
    event.listen(database.get_bind(), 'before_cursor_execute', record)
    try:
        for identifier in range(100):
            assert policies(database, 'print_job', identifier) == []
    finally:
        event.remove(database.get_bind(), 'before_cursor_execute', record)
    assert len(queries) == 2
    assert not any('SELECT scheduled_job_id FROM print_jobs' in q for q in queries)


def test_job_creation_census_requires_shared_allocator():
    """New raw/bulk/Core birth paths require explicit review and allocator wiring."""
    root = Path(__file__).resolve().parents[2] / 'backend'
    expected_raw = {
        'modules/printers/monitors/mqtt_job_lifecycle.py',
        'scripts/seed_edu_sandbox.py', 'scripts/seed_release_gate.py',
    }
    raw = set()
    constructors = {}
    for path in root.rglob('*.py'):
        relative = path.relative_to(root).as_posix()
        if relative.startswith(('tests/', 'core/schema/preserved')):
            continue
        source = path.read_text()
        tree = ast.parse(source)
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(r'\bINSERT\s+(?:OR\s+\w+\s+)?INTO\s+["`]?jobs\b', node.value, re.I):
                if relative == 'core/job_identity.py':
                    continue
                raw.add(relative)
                parent = parents.get(node)
                while parent is not None and not isinstance(parent, ast.Call):
                    parent = parents.get(parent)
                assert isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id == 'insert_job', relative
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
                assert name not in ('bulk_save_objects', 'bulk_insert_mappings'), relative
                assert not (name == 'insert' and (any(isinstance(a, ast.Name) and a.id == 'Job' for a in node.args)
                    or 'Job.__table__' in ast.unparse(node.func))), relative
                if name == 'Job':
                    assert not any(k.arg == 'id' for k in node.keywords), relative
                    constructors[relative] = constructors.get(relative, 0) + 1
    assert raw == expected_raw
    assert constructors == {
        'modules/jobs/routes/jobs_crud.py': 3,
        'modules/jobs/routes/presets.py': 1,
        'modules/jobs/queue_creation.py': 1,
        'modules/jobs/repeat.py': 1,
        'modules/orders/routes/orders_crud.py': 1,
        'modules/archives/routes/archives_crud.py': 1,
        'modules/organizations/education_submission_graph.py': 1,
    }


def test_postgres_creation_leaves_native_sequence_allocation_unchanged():
    from types import SimpleNamespace
    from core.job_identity import before_job_insert
    class Cursor:
        def __init__(self):
            self.calls = []
        def execute(self, statement, values):
            self.calls.append((statement, values))
            return self
    connection = SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
    job = SimpleNamespace(id=None)
    before_job_insert(None, connection, job)
    assert job.id is None
    cursor = Cursor()
    sql = 'INSERT INTO jobs (item_name) VALUES (?) RETURNING id'
    assert insert_job(cursor, sql, ('part',), postgres=True) is cursor
    assert cursor.calls == [(sql, ('part',))]


@pytest.mark.parametrize('path,actions', [
    ('single', ['job.deleted']), ('bulk', ['bulk_delete']),
    ('retention', []), ('organization', []),
])
def test_unmarked_real_deletion_paths_keep_original_audit_actions(database, monkeypatch, path, actions):
    import asyncio
    from tests.test_contracts.test_controlled_parts import actor
    from modules.jobs.routes import jobs_crud
    from modules.organizations.education_policy import assert_generic_job_mutation_allowed
    monkeypatch.setattr(jobs_crud, 'assert_generic_job_mutation_allowed', assert_generic_job_mutation_allowed)
    if path == 'single':
        jobs_crud.delete_job(1, actor(), database)
    elif path == 'bulk':
        result = asyncio.run(jobs_crud.bulk_update_jobs({'job_ids': [1], 'action': 'delete'}, actor(), database))
        assert result['affected'] == 1
    elif path == 'retention':
        from modules.system.routes_config import run_retention_cleanup
        database.execute(text("UPDATE jobs SET status='completed',updated_at='2000-01-01' WHERE id=1"))
        database.execute(text("INSERT INTO system_config(key,value) VALUES('data_retention',:value)"),
            {'value': '{"completed_jobs_days":1,"audit_logs_days":0,"timelapses_days":0,"alert_history_days":0}'})
        database.commit()
        asyncio.run(run_retention_cleanup(actor(2), database))
    else:
        from modules.jobs.services import delete_org_jobs
        delete_org_jobs(database, 1)
        database.commit()
    assert database.execute(text('SELECT id FROM jobs WHERE id=1')).first() is None
    assert list(database.execute(text('SELECT action FROM audit_logs ORDER BY id')).scalars()) == actions
    assert database.execute(text('SELECT count(*) FROM controlled_job_tombstones')).scalar_one() == 0


def test_sqlite_backup_preserves_deleted_job_high_water(tmp_path):
    from modules.system.backup_service import create_online_backup
    engine = create_engine('sqlite:///' + str(tmp_path / 'original.db'))
    bootstrap_database(engine)
    with Session(engine) as db:
        last = new_job(db)
        db.execute(text('DELETE FROM jobs'))
        db.commit()
    candidate, _ = create_online_backup(str(engine.url))
    engine.dispose()
    restored = create_engine('sqlite:///' + str(candidate))
    try:
        bootstrap_database(restored)
        with Session(restored) as db:
            assert new_job(db) == last + 1
            db.commit()
    finally:
        restored.dispose()
        candidate.unlink()
