"""Regressions for the first mark-failed review; owned local databases only."""
import pytest
from sqlalchemy import event, text

from tests.test_contracts.test_mark_print_failed import (
    client, correction_client, token_client, review_db, mark, undo, BODY, URL,
)


@pytest.mark.parametrize('change', ['end_time', 'deleted_job', 'requeued_job', 'later_print', 'later_print_deducted'])
def test_undo_survives_mutable_job_and_end_time(client, review_db, change):
    from modules.jobs.cost_actual import load_attempts
    assert mark(client).status_code == 200
    if change == 'end_time':
        review_db.execute(text("UPDATE print_jobs SET ended_at='2026-11-14 11:00:03' WHERE id=902"))
    elif change == 'deleted_job':
        review_db.execute(text('DELETE FROM jobs WHERE id=901'))
    else:
        from core.print_attempts import bump_print_attempt, sqlalchemy_runner
        bump_print_attempt(sqlalchemy_runner(review_db), 901)
        review_db.execute(text("UPDATE jobs SET status='pending' WHERE id=901"))
        if change in ('later_print', 'later_print_deducted'):
            review_db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at) VALUES(904,9,901,'running',CURRENT_TIMESTAMP)"))
    if change == 'later_print_deducted':
        from core.filament_ledger import record_job_filament
        from core.print_attempts import sqlalchemy_runner
        assert record_job_filament(sqlalchemy_runner(review_db), 901, source='archive', print_job_id=904, grams_by_spool={901: 10}).deducted
    review_db.commit()
    c, issue = client
    result = c.get(URL, headers=issue(['read:jobs']))
    assert result.status_code == 200
    assert result.json()['failure_correction']['reason'] == BODY['reason']
    if change != 'deleted_job':
        assert load_attempts(review_db, [901])[901][0]['usage'] is None
    stock_before = list(review_db.execute(text('SELECT * FROM spools')))
    usage_before = list(review_db.execute(text('SELECT * FROM spool_usage')))
    job_before = review_db.execute(text('SELECT * FROM jobs WHERE id=901')).first()
    response = undo(client)
    assert response.status_code == 200, response.text
    assert tuple(review_db.execute(text('SELECT status,filament_used_g FROM print_jobs WHERE id=902')).one()) == ('cancelled', 50)
    assert tuple(review_db.execute(text('SELECT status,filament_used_grams FROM print_archives WHERE id=903')).one()) == ('cancelled', 50)
    assert list(review_db.execute(text('SELECT * FROM spools'))) == stock_before
    assert list(review_db.execute(text('SELECT * FROM spool_usage'))) == usage_before
    if change != 'end_time':
        assert review_db.execute(text('SELECT * FROM jobs WHERE id=901')).first() == job_before
    else:
        assert str(review_db.execute(text('SELECT ended_at FROM print_jobs WHERE id=902')).scalar_one()) == '2026-11-14 11:00:03'


@pytest.mark.parametrize('size', [1, 50])
def test_annotation_is_one_batch_and_never_calls_mutation_guards(client, review_db, monkeypatch, size):
    import modules.jobs.print_failure as service
    assert mark(client).status_code == 200
    saved = service.record(review_db, 902)
    rows = []
    for offset in range(size):
        pid = 1000 + offset
        review_db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at,ended_at) VALUES(:id,9,'failed',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"), {'id': pid})
        row = dict(review_db.execute(text('SELECT * FROM print_jobs WHERE id=:id'), {'id': pid}).mappings().one())
        copy = dict(saved, birth=row['economic_birth_nonce'], started_at=str(row['started_at']), ended_at=str(row['ended_at']), job_id=None)
        service._save(review_db, pid, copy)
        rows.append(row)
    review_db.commit()
    def forbidden(*args, **kwargs):
        pytest.fail('A read must not run mutation guards')
    monkeypatch.setattr(service, '_load', forbidden)
    queries = []
    def capture(conn, cursor, statement, parameters, context, many):
        queries.append(statement)
    event.listen(review_db.bind, 'before_cursor_execute', capture)
    try:
        annotated = service.annotate(review_db, rows, user={'id': 3, 'role': 'admin', 'group_id': 1})
    finally:
        event.remove(review_db.bind, 'before_cursor_execute', capture)
    assert all(row['failure_correction']['note'] == BODY['note'] for row in annotated)
    assert len(queries) == 2  # one permission lookup, one bounded provenance batch


def test_read_provenance_ignores_job_mutation_guard(client, review_db, monkeypatch):
    from fastapi import HTTPException
    import modules.jobs.print_failure as service
    assert mark(client).status_code == 200
    def refuse(*args):
        raise HTTPException(409, 'Job cannot be mutated')
    monkeypatch.setattr(service, 'assert_generic_job_mutation_allowed', refuse)
    c, issue = client
    result = c.get(URL, headers=issue(['read:jobs']))
    assert result.status_code == 200
    assert result.json()['failure_correction']['note'] == BODY['note']


def test_job_timestamp_advances_and_undo_restores_it(client, review_db):
    review_db.execute(text("UPDATE jobs SET updated_at='2020-01-01 00:00:00' WHERE id=901")); review_db.commit()
    assert mark(client).status_code == 200
    assert str(review_db.execute(text('SELECT updated_at FROM jobs WHERE id=901')).scalar_one()) > '2020-01-01 00:00:00'
    assert undo(client).status_code == 200
    assert str(review_db.execute(text('SELECT updated_at FROM jobs WHERE id=901')).scalar_one()) == '2020-01-01 00:00:00'


def test_guillemet_note_is_valid_plain_text(client):
    assert mark(client, dict(BODY, note='‹5 g fed')).status_code == 200


def test_batch_annotation_keeps_controlled_archive_audience(client, review_db):
    from tests.test_contracts.test_home_assistant_status import _mark_controlled
    _mark_controlled(review_db, 901, member=True)
    review_db.commit()
    assert mark(client).status_code == 200
    review_db.execute(text('DELETE FROM controlled_audience_members WHERE user_id=3'))
    review_db.commit()
    c, issue = client
    result = c.get(URL, headers=issue(['read:jobs']))
    assert result.status_code == 404
    assert BODY['note'] not in result.text
    from modules.jobs.print_failure import annotate
    row = dict(review_db.execute(text('SELECT * FROM print_jobs WHERE id=902')).mappings().one())
    assert annotate(review_db, [row], user={'id': 3, 'role': 'admin', 'group_id': 1})[0]['failure_correction'] is None
