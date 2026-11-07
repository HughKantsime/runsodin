"""Monotone identity guards and alias correlation on both supported dialects."""
import importlib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
from core.controlled_parts import _file_rules, selected_file_sql
from tests.test_contracts.test_controlled_parts import database
from tests.test_contracts.test_controlled_round8 import clock_db, native_pg, add_job, add_rule

HISTORY = importlib.import_module('core.schema.migrations.055_controlled_history')


@pytest.mark.parametrize('statement', [
    'DELETE FROM job_id_high_water',
    'UPDATE job_id_high_water SET high_water=0',
    'INSERT INTO job_id_high_water VALUES(1,0) ON CONFLICT(singleton) DO UPDATE SET high_water=excluded.high_water',
])
def test_high_water_rejects_manual_rewind(database, statement):
    db = database
    db.execute(text("INSERT INTO jobs(id,item_name) VALUES(9001,'untraced')"))
    db.execute(text('DELETE FROM jobs WHERE id=9001'))
    db.commit()
    with pytest.raises(IntegrityError, match='high-water'):
        db.execute(text(statement))
    db.rollback()
    assert db.execute(text('SELECT high_water FROM job_id_high_water')).scalar_one() == 9001
    db.execute(text('UPDATE job_id_high_water SET high_water=high_water'))
    db.execute(text('UPDATE job_id_high_water SET high_water=9002'))
    db.commit()
    bootstrap_database(db.get_bind())
    from modules.jobs.models import Job
    job = Job(item_name='next')
    db.add(job); db.flush()
    assert job.id == 9003


@pytest.mark.parametrize('entrypoint', ['boot', 'migration'])
def test_missing_row_with_surviving_guards_is_not_rebuilt(database, entrypoint):
    db = database
    db.execute(text("INSERT INTO jobs(id,item_name) VALUES(9001,'untraced')"))
    db.execute(text('DELETE FROM jobs WHERE id=9001'))
    db.execute(text('DROP TRIGGER jobs_high_water_prevent_delete'))
    db.execute(text('DELETE FROM job_id_high_water'))
    db.commit()
    with pytest.raises(MigrationError, match='Referenced IDs alone cannot safely rebuild') as error:
        if entrypoint == 'boot':
            bootstrap_database(db.get_bind())
        else:
            HISTORY.initialize_job_ids(db.connection())
    assert 'INSERT INTO job_id_high_water' in str(error.value)
    assert db.execute(text('SELECT count(*) FROM job_id_high_water')).scalar_one() == 0
    db.execute(text('INSERT INTO job_id_high_water VALUES(1,9001)'))
    db.commit()
    bootstrap_database(db.get_bind())
    HISTORY.validate(db.connection())
    assert db.execute(text('SELECT high_water FROM job_id_high_water')).scalar_one() == 9001


def test_file_rules_inner_aliases_cannot_bind_outer_expression(clock_db):
    db = clock_db
    db.execute(text("INSERT INTO print_files(id,filename,stored_path) VALUES(11,'secret','/same'),(12,'public','/public'),(13,'alias','/same')"))
    add_job(db, 5, 11)
    add_rule(db, 'files', 11)
    add_rule(db, 'jobs', 5)
    for alias in ('cp', 'marked', 'candidate', 'CP', 'cp_inner', 'marked_inner', 'candidate_inner'):
        rules = _file_rules(alias + '.id')
        rows = db.execute(text(f'SELECT {alias}.id FROM print_files {alias} '
                               f'WHERE EXISTS ({rules}) ORDER BY {alias}.id')).scalars().all()
        assert rows == [11, 13], alias


def test_selected_file_fallback_cannot_capture_candidate_alias(clock_db):
    db = clock_db
    db.execute(text("INSERT INTO models(id,name) VALUES(21,'first'),(22,'second')"))
    db.execute(text("INSERT INTO print_files(id,filename,model_id) VALUES(11,'old',21),(12,'new',21),(13,'other',22)"))
    for jid, model in ((5, 21), (6, 22)):
        add_job(db, jid)
        db.execute(text('UPDATE jobs SET model_id=:model WHERE id=:id'), {'model': model, 'id': jid})
    for alias in ('cj', 'cm', 'cs', 'candidate', 'CANDIDATE', 'candidate_inner'):
        selected = selected_file_sql(alias + '.id')
        assert db.execute(text(f'SELECT ({selected}) FROM jobs {alias} ORDER BY {alias}.id')).scalars().all() == [12, 13]
    # This outer reference occurs inside the fallback subquery itself.
    selected = selected_file_sql('candidate.model_id')
    rows = db.execute(text('SELECT (' + selected + ') FROM '
        '(SELECT id AS model_id FROM jobs) candidate ORDER BY candidate.model_id')).scalars().all()
    assert rows == [12, 13]
