"""Operational seeding allocates; isolated native probe exemptions stay explicit."""
import ast
from pathlib import Path
import re
import sqlite3

from core.job_identity import insert_job

ROOT = Path(__file__).resolve().parents[2]


def test_demo_seed_uses_allocator_after_deleting_the_maximum():
    tree = ast.parse((ROOT / 'ops/seed_demo_full.py').read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == 'insert_job']
    assert len(calls) == 1
    sql = calls[0].args[1].value
    # Execute the seeder's actual INSERT with its columns against owned storage.
    columns = sql.split('(', 1)[1].split(')', 1)[0].replace('\n', '').split(',')
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,' + ','.join(c.strip() for c in columns) + ')')
        db.execute('CREATE TABLE job_id_high_water(singleton INTEGER PRIMARY KEY,high_water INTEGER)')
        db.execute('INSERT INTO job_id_high_water VALUES(1,15)')
        db.execute('INSERT INTO jobs(id) VALUES(10)')
        db.execute('DELETE FROM jobs')
        result = insert_job(db, sql, (None,) * len(columns))
        assert result.lastrowid == 16


def test_raw_probe_exemptions_are_reviewed_and_not_extended_silently():
    expected = {
        'attempt_start_plan_education_native.py', 'attempt_start_plan_generic_native.py',
        'attempt_start_prerequisite_native.py', 'capture_graph_native.py',
        'certd_fixes_native.py', 'chargeback_dates_native.py', 'economic_creator_native.py',
        'education_job_birth_native.py', 'education_classes_fixtures.py', 'frame_namespace_native.py',
        'post027_restore_native.py', 'power_terminal_delivery_native.py',
        'recent_terminal_native.py', 'report_usage_native.py', 'runtime_probe.py',
        'timelapse_editor_service_native.py', 'timelapse_encoder_native.py',
        'timelapse_final_commit_native.py', 'timelapse_retired_fact_contention_native.py',
        'user_hard_delete_native.py',
    }
    found = set()
    for path in (ROOT / 'ops/database_parity').glob('*.py'):
        source = path.read_text()
        if re.search(r'INSERT\s+INTO\s+jobs\b', source, re.I):
            found.add(path.name)
            if path.name == 'runtime_probe.py':
                assert 'runtime_probe.py' in (ROOT / 'docs/CONTROLLED_PARTS.md').read_text()
            else:
                assert '# Raw job INSERT exemption:' in source, path.name
    assert found == expected
    # A future raw INSERT in the demo must use the shared allocator too.
    tree = ast.parse((ROOT / 'ops/seed_demo_full.py').read_text())
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.search(r'INSERT\s+INTO\s+jobs\b', node.value, re.I):
            call = parents[node]
            assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            assert call.func.id == 'insert_job'
