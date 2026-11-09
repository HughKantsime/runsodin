"""E3 history follows every released migration and preserves audit integrity."""
import ast
import importlib
from pathlib import Path
from sqlalchemy import create_engine, text
from core.audit_integrity import verify
from core.schema import bootstrap_database
from core.schema.bootstrap import POST027_MIGRATIONS


def test_literal_registry_orders_released_then_controlled_migrations():
    from core.schema import bootstrap
    tree = ast.parse(Path(bootstrap.__file__).read_text())
    assignments = [n for n in tree.body if isinstance(n, ast.Assign)
                   and any(getattr(t, 'id', None) == 'POST027_MIGRATIONS' for t in n.targets)]
    assert len(assignments) == 1
    assert ast.literal_eval(assignments[0].value) == POST027_MIGRATIONS
    assert [mid for mid, _ in POST027_MIGRATIONS][-4:] == [
        'python:053-term-retention', 'python:054-controlled-parts', 'python:055-controlled-history',
        'python:056-dispatch-recovery-global']


def test_history_repair_commits_a_valid_audit_chain(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'history.db'))
    bootstrap_database(engine)
    history = importlib.import_module('core.schema.migrations.055_controlled_history')
    with engine.begin() as c:
        c.execute(text("INSERT INTO jobs(id,item_name) VALUES(80,'retained')"))
        c.exec_driver_sql('DROP TRIGGER jobs_high_water_insert')
    bootstrap_database(engine)
    with engine.connect() as c:
        history.validate(c)
        assert c.execute(text('SELECT high_water FROM job_id_high_water')).scalar_one() >= 80
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='job_identity.repaired'")).scalar_one() == 1
        assert verify(c)['ok']
    engine.dispose()
