"""Correlated SQL must not capture an outer alias in its own subqueries."""
import re

import pytest
from sqlalchemy import text

from core.controlled_history import retained_media_sql
from core.controlled_parts import any_read_restrictions, selected_file_sql, tombstone_job_ids
from core.controlled_policy_clock import deadline_sql
from tests.test_contracts.test_controlled_round8 import (
    ORIGIN, clock_db, native_pg, add_job, add_rule, add_media, delete_job,
)


@pytest.mark.parametrize('alias', ['t', 'p', 'a', 'tl', 'pj', 'pa', 'TL', 'tl_inner'])
def test_retained_media_never_shadows_outer_alias(alias):
    # SQLite can resolve missing inner columns against outer scopes, masking the
    # PostgreSQL failure. Check the binding even without a native server.
    sql = retained_media_sql(alias)
    declared = re.findall(r'FROM\s+\w+\s+(\w+)\s+WHERE', sql, re.I)
    assert alias.lower() not in {name.lower() for name in declared}


@pytest.mark.parametrize('media', ['timelapse', 'print_job', 'archive'])
def test_retained_media_is_correlated_for_each_link(clock_db, media):
    db = clock_db
    for identifier in (5, 6):
        add_job(db, identifier)
        add_rule(db, 'jobs', identifier)
        delete_job(db, identifier)
    # No audit row can mask a broken media predicate or an accidental tautology.
    db.execute(text('DELETE FROM audit_logs'))
    assert not any_read_restrictions(db)
    assert tombstone_job_ids(db) == set()
    if media == 'timelapse':
        add_media(db, 1, 5, ORIGIN)
    elif media == 'print_job':
        db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at) VALUES(1,1,5,'completed','2020-01-01')"))
    else:
        db.execute(text("INSERT INTO print_archives(id,job_id,print_name,status) VALUES(1,5,'retained','completed')"))
    for alias in ('t', 'p', 'a', 'tl', 'pj', 'pa', 'TL', 'tl_inner'):
        query = f'SELECT {alias}.job_id FROM controlled_job_tombstones {alias} WHERE ' + retained_media_sql(alias)
        assert db.execute(text(query)).scalars().all() == [5]
    assert any_read_restrictions(db)
    assert tombstone_job_ids(db) == {5}


def test_selected_file_and_clock_aliases_stay_correlated(clock_db):
    db = clock_db
    db.execute(text("INSERT INTO print_files(id,filename) VALUES(11,'one'),(12,'two')"))
    for job, file_id, days in ((5, 11, 7), (6, 12, 30)):
        add_job(db, job, file_id)
        add_rule(db, 'jobs', job, days=days)
    # Every current callsite plus collisions with the helper's private names.
    for alias in ('jobs', 'j', 'a', 'cp', 't', 'dj', 'cj', 'cm', 'cs', 'CJ'):
        sql = f'SELECT ({selected_file_sql(alias + ".id")}) FROM jobs {alias} ORDER BY {alias}.id'
        assert db.execute(text(sql)).scalars().all() == [11, 12]
    for alias in ('cp', 'cc', 'CC', 'cc_inner'):
        sql = f'SELECT {deadline_sql("jobs", "job_id", alias)} FROM controlled_jobs {alias} ORDER BY {alias}.job_id'
        assert [tuple(r) for r in db.execute(text(sql))] == [(7, '[]'), (30, '[]')]


def test_all_visibility_builder_callsite_aliases(clock_db):
    """Includes nested _file_rules, _audience, media and selected-file builders."""
    from core.controlled_parts import file_visibility, job_visibility
    from core.controlled_history import media_visibility
    from tests.test_contracts.test_controlled_parts import actor
    from tests.test_contracts.test_controlled_round12 import members
    db = clock_db
    members(db)
    db.execute(text("INSERT INTO print_files(id,filename) VALUES(11,'secret'),(12,'public')"))
    add_job(db, 5, 11)
    add_job(db, 6, 12)
    add_rule(db, 'files', 11)
    for who, expected in ((actor(), [5, 6]), (actor(2), [6])):
        for alias in ('jobs', 'j', 'a'):
            source = f'jobs {alias}' if alias != 'a' else '(SELECT id AS job_id FROM jobs) a'
            key = alias + ('.job_id' if alias == 'a' else '.id')
            condition, values = job_visibility(who, alias)
            assert db.execute(text(f'SELECT {key} FROM {source} WHERE {condition} ORDER BY {key}'), values).scalars().all() == expected
        for expression, source in (('f.id', 'print_files f'), ('print_files.id', 'print_files'),
                                   ('a.print_file_id', '(SELECT id AS print_file_id FROM print_files) a')):
            condition, values = file_visibility(who, expression)
            assert db.execute(text(f'SELECT {expression} FROM {source} WHERE {condition} ORDER BY {expression}'), values).scalars().all() == ([11, 12] if who['id'] == 1 else [12])
        for expression, source in (('a.job_id', '(SELECT id AS job_id FROM jobs) a'),
                                   ('timelapses.print_job_id', '(SELECT id AS print_job_id FROM jobs) timelapses')):
            condition, values = media_visibility(who, expression)
            assert db.execute(text(f'SELECT {expression} FROM {source} WHERE {condition} ORDER BY {expression}'), values).scalars().all() == expected
