"""Boot repairs missing job identity guards, but never replaces altered guards."""
import importlib
import json
import logging
import shlex
import subprocess
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from core.schema import bootstrap_database
from core.schema.bootstrap import validate_schema
from core.schema.migrator import MigrationError
from tests.test_contracts.test_controlled_parts import database
from tests.test_contracts.test_controlled_postgres import native_pg

HISTORY = importlib.import_module('core.schema.migrations.055_controlled_history')
ACTION = 'job_identity.repaired'


@pytest.mark.parametrize('entrypoint', ['boot', 'late_rebuild'])
@pytest.mark.parametrize('damage', ['insert', 'delete', 'prevent_delete', 'prevent_lower', 'stale', 'all'])
def test_missing_guards_repaired_and_audited(database, caplog, monkeypatch, entrypoint, damage):
    db = database
    # A deleted job can survive only in legacy media or an undeclared link.
    db.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename) "
                    "VALUES(1,1,901,'old.mp4')"))
    db.execute(text('CREATE TABLE legacy_job_link(job_id INTEGER)'))
    db.execute(text('INSERT INTO legacy_job_link VALUES(902)'))
    db.execute(text('UPDATE job_id_high_water SET high_water=902'))
    def break_guards(connection):
        if damage in ('insert', 'delete', 'prevent_delete', 'prevent_lower', 'all'):
            names = HISTORY.job_id_triggers() if damage == 'all' else ['jobs_high_water_' + damage]
            for name in names:
                connection.execute(text('DROP TRIGGER ' + name))
        if damage == 'all':
            connection.execute(text('DELETE FROM job_id_high_water'))
        elif damage == 'stale':
            connection.execute(text('DROP TRIGGER jobs_high_water_prevent_lower'))
            connection.execute(text('UPDATE job_id_high_water SET high_water=1'))
        else:
            connection.execute(text('UPDATE job_id_high_water SET high_water=950'))
    if entrypoint == 'boot':
        break_guards(db.connection())
    else:
        from modules.inventory import slot_material_repair
        monkeypatch.setattr(slot_material_repair, 'repair_slot_materials', break_guards)
    db.commit()
    expected = 902 if damage in ('stale', 'all') else 950
    with caplog.at_level(logging.WARNING):
        assert bootstrap_database(db.get_bind())['applied'] == []
    monkeypatch.undo()
    HISTORY.validate(db.connection())
    assert db.execute(text('SELECT high_water FROM job_id_high_water')).scalar_one() == expected
    details = db.execute(text('SELECT details FROM audit_logs WHERE action=:action'),
                         {'action': ACTION}).scalar_one()
    assert json.loads(details)['high_water'] == expected
    assert 'Repaired SQLite job identity guards' in caplog.text
    from modules.jobs.models import Job
    job = Job(item_name='After repair')
    db.add(job)
    db.flush()
    assert job.id == expected + 1
    db.commit()
    # Restart is quiet and doesn't add an audit row for an unchanged guard.
    caplog.clear()
    assert bootstrap_database(db.get_bind())['applied'] == []
    assert db.execute(text('SELECT count(*) FROM audit_logs WHERE action=:action'),
                      {'action': ACTION}).scalar_one() == 1
    assert 'Repaired SQLite job identity guards' not in caplog.text


def test_altered_trigger_refusal_has_exact_recovery_and_no_partial_repair(database):
    db = database
    db.execute(text('DROP TRIGGER jobs_high_water_insert'))
    db.execute(text('DROP TRIGGER jobs_high_water_delete'))
    altered = HISTORY.job_id_triggers()['jobs_high_water_insert'].replace('max(high_water,', 'min(high_water,')
    db.execute(text(altered))
    db.commit()
    with pytest.raises(MigrationError) as error:
        bootstrap_database(db.get_bind())
    assert 'jobs_high_water_insert' in str(error.value)
    assert 'sqlite3 ' in str(error.value)
    assert str(db.get_bind().url.database) in str(error.value)
    assert 'DROP TRIGGER "jobs_high_water_insert";' in str(error.value)
    assert 'restart ODIN' in str(error.value)
    assert db.execute(text('SELECT count(*) FROM job_id_high_water')).scalar_one() == 1
    assert db.execute(text("SELECT sql FROM sqlite_master WHERE name='jobs_high_water_insert'")).scalar_one() == altered
    assert db.execute(text("SELECT count(*) FROM sqlite_master WHERE name='jobs_high_water_delete'")).scalar_one() == 0
    db.rollback()
    command = str(error.value).split('run: ', 1)[1].split('; then restart ODIN', 1)[0]
    subprocess.run(shlex.split(command), check=True, capture_output=True)
    assert bootstrap_database(db.get_bind())['applied'] == []
    HISTORY.validate(db.connection())


def test_postgres_repair_helper_issues_no_sql():
    from core.schema.job_identity_repair import repair_job_identity
    # No connection methods: any accidental SQLite access on PG fails this test.
    repair_job_identity(SimpleNamespace(dialect=SimpleNamespace(name='postgresql')))


def test_readiness_schema_validation_stays_read_only(database):
    db = database
    db.execute(text('PRAGMA query_only=ON'))
    try:
        validate_schema(db.connection())
    finally:
        db.execute(text('PRAGMA query_only=OFF'))


def test_postgres_boot_keeps_native_identity(native_pg):
    bootstrap_database(native_pg)
    assert bootstrap_database(native_pg)['applied'] == []
    with native_pg.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM job_id_high_water')).scalar_one() == 0
        assert connection.execute(text('SELECT count(*) FROM audit_logs WHERE action=:action'),
                                  {'action': ACTION}).scalar_one() == 0
