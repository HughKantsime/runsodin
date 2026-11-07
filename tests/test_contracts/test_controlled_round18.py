"""Round 18: SQL token boundaries, correlated audiences and bootstrap audit ownership."""
import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from core.controlled_parts import _audience, selected_file_sql
from core.schema import bootstrap_database
from tests.test_contracts.test_controlled_parts import database


def test_selected_file_rewrites_only_complete_alias_tokens(monkeypatch):
    from core import dispatch_payload_selection as selection
    monkeypatch.setattr(selection, 'SELECTED_PRINT_FILE_ID_SQL',
                        'j.id + m.id + s.id + candidate.id + items.id + '
                        'obj.id + item.id + candidate_id + mycandidate.id')
    sql = selected_file_sql('candidate.id')
    assert sql.startswith('SELECT cj.id + cm.id + cs.id + candidate_inner.id + '
                          'items.id + obj.id + item.id + candidate_id + mycandidate.id FROM ')


@pytest.mark.parametrize('alias', ['cp', 'cu', 'cam', 'CP', 'CU', 'CAM',
                                  'cp_inner', 'cu_inner', 'cam_inner'])
def test_audience_keeps_outer_rule_references(database, alias):
    db = database
    rules = f'SELECT {alias}.id AS audience_id'
    for actor_id, expected in ((1, [1]), (2, [])):
        condition, params = _audience({'id': actor_id}, rules)
        from core.controlled_sql import inner_alias
        assert ') ' + inner_alias('cp', rules) + ' WHERE' in condition
        assert 'FROM users ' + inner_alias('cu', rules) + ' JOIN' in condition
        assert 'JOIN controlled_audience_members ' + inner_alias('cam', rules) + ' ON' in condition
        rows = db.execute(text(f'SELECT {alias}.id FROM controlled_audiences {alias} '
                               f'WHERE {condition} ORDER BY {alias}.id'), params).scalars().all()
        assert rows == expected


@pytest.mark.parametrize('entrypoint', ['migration', 'late_boot'])
def test_job_identity_audit_commits_and_rolls_back_with_bootstrap(database, monkeypatch, entrypoint):
    from core.schema import job_identity_repair
    db = database
    if entrypoint == 'migration':
        db.execute(text('DROP TRIGGER jobs_high_water_insert'))
        db.commit()
    else:
        from modules.inventory import slot_material_repair
        def damage(connection):
            connection.exec_driver_sql('UPDATE job_id_high_water SET high_water=high_water')
            connection.exec_driver_sql('DROP TRIGGER jobs_high_water_insert')
        monkeypatch.setattr(slot_material_repair, 'repair_slot_materials', damage)
    # A separate engine/DBAPI connection cannot see uncommitted bootstrap writes.
    observer = create_engine(db.get_bind().url, poolclass=NullPool)
    audit = text("SELECT details FROM audit_logs WHERE action='job_identity.repaired'")
    repair = job_identity_repair.repair_job_identity
    def fail_after_repair(connection):
        repair(connection)
        if connection.execute(audit).first() is None:
            return  # late_boot damage has not happened yet
        assert connection.execute(audit).scalar_one()
        with observer.connect() as outside:
            assert outside.execute(audit).all() == []
        raise RuntimeError('abort bootstrap after audit flush')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(job_identity_repair, 'repair_job_identity', fail_after_repair)
            with pytest.raises(RuntimeError, match='abort bootstrap'):
                bootstrap_database(db.get_bind())
        with observer.connect() as outside:
            assert outside.execute(audit).all() == []
        bootstrap_database(db.get_bind())
        with observer.connect() as outside:
            details = json.loads(outside.execute(audit).scalar_one())
            assert details['triggers'] == ['jobs_high_water_insert']
            assert outside.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE name='jobs_high_water_insert'").scalar_one() == 1
    finally:
        observer.dispose()


def test_demo_wipe_removes_controlled_state_but_keeps_identity_floor(database):
    db = database
    db.execute(text("INSERT INTO controlled_files(file_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                    "VALUES(1,'CUI',1,'Secure',7,'test','2020-01-01')"))
    db.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                    "VALUES(1,'CUI',1,'Secure',7,'test','2020-01-01')"))
    db.execute(text("INSERT INTO controlled_job_tombstones(job_id,audience_id,label,printer_tag,retention_days,"
                    "marked_at,created_at,audit_max_id,rule_index) "
                    "VALUES(99,1,'CUI','Secure',7,'2020-01-01','2020-01-01',0,0)"))
    db.execute(text("INSERT INTO controlled_retention_clocks(resource_type,resource_id,marked_at,initial_days,changes) "
                    "VALUES('files',1,'2020-01-01',7,'[]')"))
    db.commit()
    source = Path(__file__).resolve().parents[2] / 'ops/seed_demo_full.py'
    main = next(n for n in ast.parse(source.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == 'main')
    start = next(i for i, n in enumerate(main.body) if isinstance(n, ast.Assign)
                 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'tables')
    # Execute the actual wipe list, loop and commit, never the seeder's live path.
    wipe = compile(ast.Module(body=main.body[start:start + 3], type_ignores=[]), str(source), 'exec')
    import sqlite3
    with sqlite3.connect(db.get_bind().url.database) as connection:
        connection.execute('PRAGMA foreign_keys=OFF')
        exec(wipe, {'conn': connection, 'sqlite3': sqlite3})
        for table in ('controlled_files', 'controlled_jobs', 'controlled_audiences',
                      'controlled_audience_members', 'controlled_job_tombstones',
                      'controlled_retention_clocks'):
            assert connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0, table
        assert connection.execute('SELECT high_water FROM job_id_high_water').fetchone()[0] >= 2
