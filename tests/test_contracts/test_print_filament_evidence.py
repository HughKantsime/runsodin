"""Durable claims, candidate identity, deletion and startup adoption contracts."""
import json
import re
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text

from core import filament_ledger as ledger
from core.filament_evidence_backfill import backfill, CURSOR
from core.print_attempts import sqlalchemy_runner, bind_print_attempt, detach_print_attempt, clear_job_attempts
from core.schema import bootstrap_database


@pytest.fixture
def evidence_engine():
    engine = create_engine('sqlite://')
    @event.listens_for(engine, 'connect')
    def foreign_keys(dbapi, _):
        dbapi.execute('PRAGMA foreign_keys=ON')
    bootstrap_database(engine)
    with engine.begin() as c:
        c.exec_driver_sql("INSERT INTO printers(id,name) VALUES(1,'Fixture')")
        c.exec_driver_sql("INSERT INTO filament_library(id,brand,name) VALUES(1,'Fixture','PLA')")
        c.exec_driver_sql("INSERT INTO spools(id,filament_id,remaining_weight_g,created_at) VALUES(1,1,1000,'2025-01-01'),(2,1,1000,'2025-01-01')")
        c.exec_driver_sql("INSERT INTO jobs(id,item_name,printer_id,created_at) VALUES(1,'Fixture',1,'2025-01-01')")
        c.exec_driver_sql("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,started_at,status) VALUES(1,1,1,'2026-01-01','completed'),(2,1,1,'2026-01-02','completed')")
    yield engine
    engine.dispose()


def balance(c):
    return c.exec_driver_sql('SELECT remaining_weight_g FROM spools WHERE id=1').scalar_one()


@pytest.mark.parametrize('source', ['archive', 'moonraker', 'operator_complete', 'settlement'])
def test_each_deducer_rollback_keeps_stock_and_evidence_atomic(evidence_engine, source):
    with evidence_engine.connect() as c:
        transaction = c.begin()
        run = sqlalchemy_runner(c)
        if source == 'settlement':
            result = ledger.record_settlement(run, 1, 1, grams_by_spool={1: 40}, counted_usage_ids=[])
        else:
            result = ledger.record_job_filament(run, 1, source=source, print_job_id=1, grams_by_spool={1: 40})
        assert result.deducted
        assert ledger.print_evidence(run, [1])[1][0] == 'final'
        transaction.rollback()
        assert balance(c) == 1000
        assert c.exec_driver_sql('SELECT COUNT(*) FROM spool_usage').scalar_one() == 0
        assert c.exec_driver_sql('SELECT COUNT(*) FROM print_filament_evidence').scalar_one() == 0


def test_two_deducers_one_print_one_claim(evidence_engine):
    with evidence_engine.begin() as c:
        run = sqlalchemy_runner(c)
        assert ledger.record_print_usage(run, 1, source='archive', grams_by_spool={1: 45}).deducted
        assert ledger.record_print_usage(run, 1, source='moonraker', grams_by_spool={2: 40}).state == 'already_deducted'
        assert c.exec_driver_sql('SELECT COUNT(*) FROM print_filament_evidence WHERE claim=1').scalar_one() == 1
        assert balance(c) == 955


@pytest.mark.parametrize('ending', ['delete', 'detach'])
def test_manual_detach_relink_complete_then_delete_or_detach_deducts_once(evidence_engine, ending):
    with evidence_engine.begin() as c:
        c.exec_driver_sql('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=2')
        run = sqlalchemy_runner(c)
        ledger.record_manual_usage(run, 1, 5, job_id=1)
        detach_print_attempt(run, 1, 1)
        c.exec_driver_sql('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=1')
        c.exec_driver_sql('UPDATE print_jobs SET scheduled_job_id=1 WHERE id=1')
        bind_print_attempt(run, 1, 1)
        assert ledger.record_job_filament(run, 1, source='operator_complete', grams_by_spool={1: 40}).deducted
        assert balance(c) == 955
        if ending == 'delete':
            c.exec_driver_sql('DELETE FROM jobs WHERE id=1')
            clear_job_attempts(run, 1)
            assert c.exec_driver_sql('SELECT scheduled_job_id FROM print_jobs WHERE id=1').scalar_one() == 1
        else:
            detach_print_attempt(run, 1, 1)
            c.exec_driver_sql('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=1')
        assert ledger.record_settlement(run, None, 1, grams_by_spool={1: 40}, counted_usage_ids=[]).state == 'already_deducted'
        assert balance(c) == 955


def test_spool_hard_delete_keeps_print_final(evidence_engine):
    with evidence_engine.begin() as c:
        run = sqlalchemy_runner(c)
        ledger.record_print_usage(run, 1, source='archive', grams_by_spool={1: 45})
        c.exec_driver_sql('DELETE FROM spools WHERE id=1')
        assert c.exec_driver_sql('SELECT COUNT(*) FROM spool_usage').scalar_one() == 0
        state, rows = ledger.print_evidence(run, [1])[1]
        assert state == 'final' and rows[0]['grams'] == 45
        assert ledger.record_print_usage(run, 1, source='archive', grams_by_spool={2: 40}).state == 'already_deducted'


def test_sqlite_reused_usage_id_does_not_match_or_block(evidence_engine):
    with evidence_engine.begin() as c:
        run = sqlalchemy_runner(c)
        ledger.record_manual_usage(run, 1, 40, job_id=1)
        ledger.close_print(run, 1, source='settlement', reason='recorded_by_hand', counted_usage_ids=[1])
        c.exec_driver_sql('DELETE FROM spools WHERE id=1')
        c.exec_driver_sql("INSERT INTO spools(id,filament_id,remaining_weight_g) VALUES(1,1,1000)")
        ledger.record_manual_usage(run, 1, 20, job_id=1)
        rows = ledger.manual_rows_for_print(run, 1, 2)
        assert len(rows) == 1 and rows[0]['usage_id'] == 1 and rows[0]['grams'] == 20
        ledger.close_print(run, 2, source='settlement', reason='recorded_by_hand', counted_usage_ids=[1])
        assert ledger.print_evidence(run, [2])[2][0] == 'final'


@pytest.mark.parametrize('prints', [0, 1, 2])
def test_complete_with_zero_one_two_prints(evidence_engine, prints):
    with evidence_engine.begin() as c:
        c.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id>:n'), {'n': prints})
        run = sqlalchemy_runner(c)
        assert ledger.record_job_filament(run, 1, source='operator_complete', grams_by_spool={1: 40}).deducted
        assert ledger.record_job_filament(run, 1, source='operator_complete', grams_by_spool={1: 40}).state == 'already_deducted'
        assert balance(c) == 960
        assert c.exec_driver_sql('SELECT print_job_id FROM spool_usage').scalar_one() == (prints or None)
        assert c.exec_driver_sql('SELECT COUNT(*) FROM print_filament_evidence WHERE claim=1').scalar_one() == bool(prints)


@pytest.mark.parametrize('old_printer_external', [False, True])
def test_dangling_link_and_reused_job_id_do_not_cross_attribute(evidence_engine, monkeypatch, old_printer_external):
    from core.config import settings
    with evidence_engine.begin() as c:
        if old_printer_external:
            c.exec_driver_sql("INSERT INTO printers(id,name) VALUES(2,'External fixture')")
            c.exec_driver_sql('UPDATE print_jobs SET printer_id=2')
            monkeypatch.setattr(settings, 'external_filament_accounting_printers', [2])
        c.exec_driver_sql("UPDATE jobs SET created_at='2026-11-01' WHERE id=1")
        run = sqlalchemy_runner(c)
        ledger.record_manual_usage(run, 1, 40, job_id=1)
        bind_print_attempt(run, 1, 1)
        assert ledger.print_evidence(run, [1])[1] == ('none', [])
        ledger.close_print(run, 1, source='settlement', reason='nothing_extruded')
        assert ledger.record_job_filament(run, 1, source='operator_complete', grams_by_spool={1: 40}).deducted
        assert balance(c) == 920


def test_legacy_null_nonce_and_changed_start_keep_identity(evidence_engine):
    with evidence_engine.begin() as c:
        run = sqlalchemy_runner(c)
        ledger.close_print(run, 1, source='settlement', reason='nothing_extruded')
        c.exec_driver_sql("UPDATE print_jobs SET started_at='2026-02-01' WHERE id=1")
        assert ledger.print_key(run, 1) == (1, '')
        assert ledger.prior_attempt_usage(run, 1, 1) == 'marker'


def test_reused_usage_id_with_two_candidates_on_one_print_refuses_ambiguity(evidence_engine):
    with evidence_engine.begin() as c:
        run = sqlalchemy_runner(c)
        ledger.record_manual_usage(run, 1, 40, job_id=1)
        c.exec_driver_sql('DELETE FROM spools WHERE id=1')
        c.exec_driver_sql('INSERT INTO spools(id,filament_id,remaining_weight_g) VALUES(1,1,1000)')
        ledger.record_manual_usage(run, 1, 20, job_id=1)
        with pytest.raises(ledger.InvalidSettlementEvidence, match='same number'):
            ledger.manual_rows_for_print(run, 1, 1)
        with pytest.raises(ledger.InvalidSettlementEvidence, match='same number'):
            ledger.close_print(run, 1, source='settlement', reason='recorded_by_hand', counted_usage_ids=[1])
        assert ledger.print_evidence(run, [1])[1][0] == 'ask'


def test_candidate_fanout_is_not_a_claim_and_is_counted_only_once(evidence_engine):
    with evidence_engine.begin() as c:
        run = sqlalchemy_runner(c)
        ledger.record_manual_usage(run, 1, 40, job_id=1)
        assert len(ledger.manual_rows_for_print(run, 1, 1)) == len(ledger.manual_rows_for_print(run, 1, 2)) == 1
        ledger.close_print(run, 1, source='settlement', reason='recorded_by_hand', counted_usage_ids=[1])
        assert ledger.manual_rows_for_print(run, 1, 1) == ledger.manual_rows_for_print(run, 1, 2) == []
        with pytest.raises(ledger.InvalidSettlementEvidence, match='no longer available'):
            ledger.close_print(run, 2, source='settlement', reason='recorded_by_hand', counted_usage_ids=[1])


@pytest.mark.parametrize('rule', ['direct', 'archive', 'marker', 'named_marker', 'automatic', 'manual', 'unresolved', 'reconcile', 'job_marker', 'deleted', 'none'])
def test_backfill_rules_and_repeat_noop(evidence_engine, rule):
    with evidence_engine.begin() as c:
        if rule in ('direct', 'automatic', 'manual'):
            c.execute(text('INSERT INTO spool_usage(spool_id,job_id,print_job_id,weight_used_g,used_at,notes) VALUES(1,1,:pid,40,\'2026-01-01\',:notes)'),
                      {'pid': 1 if rule == 'direct' else None, 'notes': 'manual: purge' if rule == 'manual' else 'auto:archive'})
        elif rule in ('archive', 'unresolved', 'deleted'):
            c.execute(text("INSERT INTO print_archives(print_job_id,accounting_state,accounting_reason) VALUES(1,:state,:reason)"),
                      {'state': 'accounted' if rule == 'archive' else 'unresolved', 'reason': 'job_usage_unattributed' if rule == 'unresolved' else 'no_bound_spool'})
            if rule == 'deleted':
                c.exec_driver_sql('DELETE FROM jobs WHERE id=1')
        elif rule in ('marker', 'reconcile', 'named_marker', 'job_marker'):
            key = 'quota_attempt:filament:job:1:' + ('print:1' if rule == 'marker' else 'reconcile:print:1' if rule == 'reconcile' else '0')
            c.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),
                      {'key': key, 'value': json.dumps({'print_job_id': 1 if rule == 'named_marker' else None})})
    counts = backfill(evidence_engine)
    if rule in ('automatic', 'manual'):
        assert counts['unattributed' if rule == 'automatic' else 'manual'] == 2
    if rule == 'deleted':
        assert counts['job_deleted'] == 1
    with evidence_engine.connect() as c:
        state, rows = ledger.print_evidence(sqlalchemy_runner(c), [1])[1]
        expected = 'final' if rule in ('direct', 'archive', 'marker', 'named_marker') else 'none' if rule == 'none' else 'ask'
        assert state == expected
        if rule == 'manual':
            assert rows[0]['kind'] == 'manual' and ledger.prior_attempt_usage(sqlalchemy_runner(c), 1, 1) is None
        before = c.exec_driver_sql('SELECT * FROM print_filament_evidence').all()
    assert backfill(evidence_engine) == {}
    with evidence_engine.connect() as c:
        assert c.exec_driver_sql('SELECT * FROM print_filament_evidence').all() == before
        assert balance(c) == 1000


def test_backfill_error_and_cursor_resume(evidence_engine, monkeypatch):
    from core import filament_evidence_backfill as adoption
    original = adoption._print
    def broken(run, pid, markers):
        if pid == 1:
            raise ValueError('Unreadable legacy evidence')
        return original(run, pid, markers)
    monkeypatch.setattr(adoption, '_print', broken)
    assert backfill(evidence_engine, batch_size=1)['error'] == 1
    with evidence_engine.begin() as c:
        assert c.exec_driver_sql("SELECT reason FROM print_filament_evidence WHERE print_job_id=1").scalar_one() == 'backfill_error'
        c.exec_driver_sql("INSERT INTO print_jobs(id,printer_id,started_at) VALUES(3,1,'2026-01-03')")
        c.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),
                  {'key': 'quota_attempt:filament:job:1:reconcile:print:3', 'value': '{bad'})
    assert backfill(evidence_engine) == {'unknown': 1}


def test_backfill_storage_failure_stops_setup(evidence_engine, monkeypatch):
    monkeypatch.setattr(ledger, 'note_candidates', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('storage unavailable')))
    from core import filament_evidence_backfill as adoption
    monkeypatch.setattr(adoption, '_print', lambda *a: (_ for _ in ()).throw(ValueError('bad evidence')))
    with pytest.raises(RuntimeError, match='Database setup must finish'):
        backfill(evidence_engine)
    with evidence_engine.connect() as c:
        assert c.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': CURSOR}).scalar() is None


def test_backfill_restart_resumes_last_committed_batch(evidence_engine, monkeypatch):
    from core import filament_evidence_backfill as adoption
    original = adoption._print
    visited = []
    def interrupted(run, pid, markers):
        visited.append(pid)
        ledger.close_print(run, pid, source='backfill', reason='marker')
        if pid == 2:
            raise KeyboardInterrupt
        return 'closed'
    monkeypatch.setattr(adoption, '_print', interrupted)
    with pytest.raises(KeyboardInterrupt):
        backfill(evidence_engine, batch_size=1)
    with evidence_engine.connect() as c:
        assert str(c.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': CURSOR}).scalar_one()) == '1'
        assert c.exec_driver_sql('SELECT print_job_id FROM print_filament_evidence').scalars().all() == [1]
    def resumed(run, pid, markers):
        visited.append(pid)
        return original(run, pid, markers)
    monkeypatch.setattr(adoption, '_print', resumed)
    assert backfill(evidence_engine, batch_size=1) == {'none': 1}
    assert visited == [1, 2, 2]


@pytest.mark.parametrize('source', ['archive', 'moonraker', 'operator_complete', 'settlement'])
def test_zero_deduction_settlement_is_final_for_every_deducer(evidence_engine, source):
    with evidence_engine.begin() as c:
        run = sqlalchemy_runner(c)
        assert ledger.record_settlement(run, 1, 1, grams_by_spool={}, counted_usage_ids=[], reason='nothing_extruded').state == 'nothing_deducted'
        if source == 'settlement':
            result = ledger.record_settlement(run, 1, 1, grams_by_spool={1: 40}, counted_usage_ids=[])
        else:
            result = ledger.record_job_filament(run, 1, source=source, print_job_id=1, grams_by_spool={1: 40})
        assert result.state == 'already_deducted'
        assert balance(c) == 1000
        assert c.exec_driver_sql('SELECT COUNT(*) FROM spool_usage').scalar_one() == 0


def test_runtime_never_reads_deleted_print_markers_and_links_carry_candidates():
    root = Path(__file__).resolve().parents[2] / 'backend'
    assert not (root / 'core/filament_read_evidence.py').exists()
    for path in [root / 'core/filament_ledger.py', root / 'modules/archives/settlement_data.py']:
        source = path.read_text()
        assert 'filament_settlement:print:' not in source
        assert 'filament:job:' not in source
    for path in root.rglob('*.py'):
        if any(part.startswith('preserved') for part in path.parts):
            continue
        source = path.read_text()
        if re.search(r'UPDATE\s+print_jobs\s+SET\s+scheduled_job_id\s*=\s*[:?]', source):
            if path.name != 'order_production.py':  # Locks without changing the link.
                assert 'bind_print_attempt' in source, path


def test_retention_500_deletions_with_50k_evidence_rows(evidence_engine):
    def run_deletions():
        with evidence_engine.begin() as c:
            c.execute(text("INSERT INTO jobs(id,item_name,created_at) VALUES(:id,'Retention','2025-01-01')"),
                      [{'id': i} for i in range(10000, 10500)])
        statements = []
        def capture(_c, _cursor, sql, *_):
            if 'print_filament_evidence' in sql:
                statements.append(sql)
        event.listen(evidence_engine, 'before_cursor_execute', capture)
        start = time.perf_counter()
        try:
            with evidence_engine.begin() as c:
                for jid in range(10000, 10500):
                    c.execute(text('DELETE FROM jobs WHERE id=:id'), {'id': jid})
                    clear_job_attempts(sqlalchemy_runner(c), jid)
        finally:
            duration = time.perf_counter() - start
            event.remove(evidence_engine, 'before_cursor_execute', capture)
        assert not statements
        return duration
    empty = run_deletions()
    with evidence_engine.begin() as c:
        c.execute(text("INSERT INTO print_filament_evidence(print_job_id,kind,source) VALUES(:id,'closed','backfill')"),
                  [{'id': i} for i in range(50000)])
    populated = run_deletions()
    print(f'Retention 500 jobs: empty={empty:.3f}s; 50k evidence={populated:.3f}s')
    assert populated <= empty * 1.5


def test_backfill_50k_usage_rows(evidence_engine):
    with evidence_engine.begin() as c:
        c.exec_driver_sql('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=2')
        c.execute(text("INSERT INTO spool_usage(spool_id,job_id,weight_used_g,used_at,notes) VALUES(1,1,1,'2026-01-01','auto:operator_complete')"), [{} for _ in range(50000)])
    start = time.perf_counter()
    backfill(evidence_engine)
    elapsed = time.perf_counter() - start
    print(f'Backfill 50k usage rows: {elapsed:.3f}s')
    with evidence_engine.connect() as c:
        assert c.exec_driver_sql('SELECT COUNT(*) FROM print_filament_evidence').scalar_one() == 50000
