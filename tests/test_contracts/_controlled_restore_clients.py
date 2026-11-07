"""Owned client/DB doubles; real staging, apply and identity preservation still run.

Archive admission and schema checks have dedicated contracts. This fixture models
only their accepted boundary so sequence orchestration needs neither Docker nor
host PostgreSQL clients. Real archives are covered by controlled_restore_native.
"""
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.dialects.postgresql import dialect


def install(monkeypatch, backup):
    state = {'jobs': [], 'sequence': [1, False]}
    calls = []

    class Result:
        def __init__(self, value=None):
            self.value = value

        def one(self):
            return self.value

        def scalar_one(self):
            return self.value

    class Connection:
        dialect = dialect()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def commit(self):
            pass

        def close(self):
            pass

        def execute(self, statement, params=None):
            sql = str(statement)
            if 'SELECT n.nspname,c.relname' in sql:
                return Result(('public', 'jobs_id_seq'))
            if sql == 'SELECT COALESCE(max(id),0) FROM public.jobs':
                return Result(max(state['jobs'], default=0))
            if sql.startswith('SELECT setval('):
                assert params['sequence'] == '"public"."jobs_id_seq"'
                state['sequence'] = [params['value'], params['called']]
                calls.append(('preserve', tuple(state['sequence'])))
                return Result(params['value'])
            if sql == 'SELECT pg_backend_pid()':
                return Result(123)
            if (sql.startswith('SELECT pg_advisory_') or sql == "SET lock_timeout = '5s'"
                    or sql.startswith('INSERT INTO audit_logs')):
                return Result()
            raise AssertionError('Unexpected contract SQL: ' + sql)

        def exec_driver_sql(self, sql):
            if sql == 'SELECT last_value,is_called FROM "public"."jobs_id_seq"':
                return Result(tuple(state['sequence']))
            assert sql == 'TRUNCATE public."jobs" RESTART IDENTITY', sql
            calls.append(('truncate', tuple(state['sequence'])))
            state.update(jobs=[], sequence=[1, False])

    engine = SimpleNamespace(begin=Connection, connect=Connection, dispose=lambda: None)
    monkeypatch.setattr(backup, 'create_database_engine', lambda *a, **k: engine)
    monkeypatch.setattr(backup, 'canonical_tables', lambda: {'jobs'})
    monkeypatch.setattr(backup, 'inspect', lambda c: SimpleNamespace(get_table_names=lambda **k: ['jobs']))
    for name in ('validate_live_postgres_objects', 'validate_schema', 'bootstrap_database',
                 '_assert_no_target_sessions', '_assert_no_external_dependencies', '_insert_postgres_restore_audit'):
        monkeypatch.setattr(backup, name, lambda *a, **k: None)

    def metadata(archive, *args, **kwargs):
        return {'sha256': backup._sha256(archive), 'size_bytes': archive.stat().st_size}

    monkeypatch.setattr(backup, 'validate_postgres_restore', metadata)
    monkeypatch.setattr(backup, '_inspect_compatible_archive', metadata)

    def restore_list(archive, target_url, password_file, directory, **kwargs):
        path = directory / 'owned.list'
        path.write_text('owned jobs data and sequence')
        return path

    monkeypatch.setattr(backup, '_validated_restore_list', restore_list)
    from modules.system import pre026_restore_stage, postgres_canonical_executables

    @contextmanager
    def prepared(archive, *args):
        yield archive

    monkeypatch.setattr(pre026_restore_stage, 'prepared_archive', prepared)
    for name in ('remove_trusted_guards', 'install_trusted_guards'):
        monkeypatch.setattr(postgres_canonical_executables, name, lambda *a, **k: None)

    def client(command, database_url, password_file, directory):
        calls.append((command[0], tuple(state['sequence'])))
        if command[0] == 'pg_dump':
            Path(command[command.index('--file') + 1]).write_text(json.dumps(state, indent=2))
        else:
            assert command[0] == 'pg_restore'
            assert {'--data-only', '--single-transaction', '--use-list'} <= set(command)
            assert Path(command[command.index('--use-list') + 1]).is_file()
            state.update(json.loads(Path(command[-1]).read_text()))
        return ''

    # Same client boundary used by main's restore contracts; no executable lookup.
    monkeypatch.setattr(backup, '_run_pg_tool', client)

    def create_jobs(count):
        created = []
        for _ in range(count):
            value, called = state['sequence']
            identifier = value + int(called)
            state['sequence'] = [identifier, True]
            state['jobs'].append(identifier)
            created.append(identifier)
        return created

    return state, calls, create_jobs, Connection
