"""
O.D.I.N. MQTT auto-linking: name matching and layer-count fallback.

Drives the real Bambu job-start path (modules.printers.monitors.mqtt_job_lifecycle.
record_job_started) against the bootstrapped production schema. The previous version
of this file ran a removed ``mqtt_monitor.PrinterMonitor._job_started`` against a
hand-written six-table schema; the current monitor needs the real schema (attempt
observation, dispatch claims, education guards), so those tests failed on main for
harness reasons, not because of the linking behaviour. Each original scenario is kept.
Only the MQTT transport is absent; Python clocks are real.
"""
from contextlib import contextmanager

import pytest
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


@pytest.fixture
def linking(review_db, monkeypatch):
    from core.db_utils import _ConnectionAdapter
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.printers.monitors import mqtt_job_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    engine = review_db.get_bind()

    @contextmanager
    def isolated_db():
        connection = engine.raw_connection()
        try:
            yield _ConnectionAdapter(connection, False)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', isolated_db)

    def setup(models=(), print_files=(), jobs=()):
        for m in models:
            review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(:id,:name,1)"), m)
        for pf in print_files:
            review_db.execute(text("INSERT INTO print_files(model_id,filename,layer_count,org_id) "
                                   "VALUES(:model_id,:filename,:layer_count,1)"), pf)
        for j in jobs:
            review_db.execute(text(
                "INSERT INTO jobs(id,model_id,item_name,printer_id,status,charged_to_org_id,quantity,hold,priority) "
                "VALUES(:id,:model_id,:item_name,:printer_id,:status,1,1,FALSE,3)"),
                {'status': 'pending', **j})
        review_db.commit()

    def start(printer_id, state):
        state = {'gcode_file': 'Metadata/plate_1.gcode', 'job_id': 'linking-task', **state}
        new_job_id, linked, _ = mqtt_job_lifecycle.record_job_started(
            printer_id, 'TestPrinter', state, lambda **kwargs: None, lambda: None)
        return new_job_id, linked

    def job_status(job_id):
        review_db.expire_all()
        return review_db.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': job_id}).scalar_one()

    return setup, start, job_status, review_db


class TestNameMatching:
    """Strategy 1: substring match on filename, item_name, or model name."""

    def test_exact_model_name_match(self, linking):
        setup, start, _, _ = linking
        setup(models=[{'id': 1, 'name': 'baby_yoda'}],
              print_files=[{'model_id': 1, 'filename': 'baby_yoda.3mf', 'layer_count': 100}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'Baby Yoda Build', 'printer_id': 9}])
        assert start(9, {'subtask_name': 'baby_yoda.3mf', 'total_layer_num': 100})[1] == 10

    def test_substring_target_in_job_base(self, linking):
        """MQTT sends a longer name that contains the DB model name."""
        setup, start, _, _ = linking
        setup(models=[{'id': 1, 'name': 'baby_yoda_v2_final'}],
              print_files=[{'model_id': 1, 'filename': 'baby_yoda_v2_final.3mf', 'layer_count': 50}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'Yoda', 'printer_id': 9}])
        assert start(9, {'subtask_name': 'baby_yoda_v2_final_plate1.3mf', 'total_layer_num': 50})[1] == 10

    def test_substring_job_base_in_target(self, linking):
        """MQTT sends 'widget', DB has 'widget_left_arm'."""
        setup, start, _, _ = linking
        setup(models=[{'id': 1, 'name': 'widget_left_arm'}],
              print_files=[{'model_id': 1, 'filename': 'widget_left_arm.3mf', 'layer_count': 80}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'Widget Arms', 'printer_id': 9}])
        assert start(9, {'subtask_name': 'widget.3mf', 'total_layer_num': 80})[1] == 10

    def test_case_insensitive(self, linking):
        setup, start, _, _ = linking
        setup(models=[{'id': 1, 'name': 'mypart'}],
              print_files=[{'model_id': 1, 'filename': 'mypart.3mf', 'layer_count': 60}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'mypart job', 'printer_id': 9}])
        assert start(9, {'subtask_name': 'MyPart.3mf', 'total_layer_num': 60})[1] == 10

    def test_extension_stripping(self, linking):
        setup, start, _, _ = linking
        setup(models=[{'id': 1, 'name': 'bracket'}],
              print_files=[{'model_id': 1, 'filename': 'bracket.gcode', 'layer_count': 40}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'Bracket Print', 'printer_id': 9}])
        assert start(9, {'subtask_name': 'bracket.3mf', 'total_layer_num': 40})[1] == 10

    def test_only_matches_same_printer(self, linking):
        setup, start, _, _ = linking
        setup(models=[{'id': 1, 'name': 'part_a'}],
              print_files=[{'model_id': 1, 'filename': 'part_a.3mf', 'layer_count': 30}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'Part A', 'printer_id': 10}])
        new_job_id, linked = start(9, {'subtask_name': 'part_a.3mf', 'total_layer_num': 30})
        assert new_job_id is not None and linked is None  # the print is recorded; nothing is linked

    def test_only_matches_scheduled_or_pending(self, linking):
        setup, start, _, _ = linking
        setup(models=[{'id': 1, 'name': 'done_part'}],
              print_files=[{'model_id': 1, 'filename': 'done_part.3mf', 'layer_count': 20}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'Done Part', 'printer_id': 9, 'status': 'completed'}])
        new_job_id, linked = start(9, {'subtask_name': 'done_part.3mf', 'total_layer_num': 20})
        assert new_job_id is not None and linked is None  # the print is recorded; nothing is linked


class TestLayerCountMatching:
    """Strategy 2: unique layer count match as fallback."""

    def test_unique_layer_match(self, linking):
        setup, start, _, _ = linking
        setup(models=[{'id': 1, 'name': 'alpha'}],
              print_files=[{'model_id': 1, 'filename': 'alpha.3mf', 'layer_count': 157}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'Alpha Build', 'printer_id': 9}])
        assert start(9, {'subtask_name': 'completely_different_name.3mf', 'total_layer_num': 157})[1] == 10

    def test_multiple_layer_matches_no_link(self, linking):
        setup, start, _, _ = linking
        setup(models=[{'id': 1, 'name': 'part_x'}, {'id': 2, 'name': 'part_y'}],
              print_files=[{'model_id': 1, 'filename': 'part_x.3mf', 'layer_count': 200},
                           {'model_id': 2, 'filename': 'part_y.3mf', 'layer_count': 200}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'Part X', 'printer_id': 9},
                    {'id': 11, 'model_id': 2, 'item_name': 'Part Y', 'printer_id': 9}])
        new_job_id, linked = start(9, {'subtask_name': 'unknown_part.3mf', 'total_layer_num': 200})
        assert new_job_id is not None and linked is None  # the print is recorded; nothing is linked

    def test_zero_layers_skips_strategy2(self, linking):
        setup, start, _, _ = linking
        setup(models=[{'id': 1, 'name': 'zeropart'}],
              print_files=[{'model_id': 1, 'filename': 'zeropart.3mf', 'layer_count': 0}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'Zero Part', 'printer_id': 9}])
        new_job_id, linked = start(9, {'subtask_name': 'nomatch.3mf', 'total_layer_num': 0})
        assert new_job_id is not None and linked is None  # the print is recorded; nothing is linked


class TestNoCandidates:
    def test_no_scheduled_jobs(self, linking):
        """No jobs at all: a print_jobs record is still created, nothing is linked."""
        _, start, _, db = linking
        before = db.execute(text('SELECT COUNT(*) FROM print_jobs')).scalar_one()
        db.rollback()  # release the read transaction before the monitor's own writer
        new_job_id, linked = start(9, {'subtask_name': 'orphan.3mf', 'total_layer_num': 50})
        assert new_job_id is not None and linked is None
        db.expire_all()
        assert db.execute(text('SELECT COUNT(*) FROM print_jobs')).scalar_one() == before + 1


class TestStatusUpdate:
    def test_linked_job_set_to_printing(self, linking):
        setup, start, job_status, db = linking
        setup(models=[{'id': 1, 'name': 'status_test'}],
              print_files=[{'model_id': 1, 'filename': 'status_test.3mf', 'layer_count': 75}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'Status Test', 'printer_id': 9}])
        new_job_id, linked = start(9, {'subtask_name': 'status_test.3mf', 'total_layer_num': 75})
        assert linked == 10 and job_status(10) == 'printing'
        db.expire_all()
        assert db.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'), {'id': new_job_id}).scalar_one() == 10

    def test_unlinked_job_status_unchanged(self, linking):
        setup, start, job_status, _ = linking
        setup(models=[{'id': 1, 'name': 'nolink'}],
              print_files=[{'model_id': 1, 'filename': 'nolink.3mf', 'layer_count': 50}],
              jobs=[{'id': 10, 'model_id': 1, 'item_name': 'NoLink', 'printer_id': 9}])
        start(9, {'subtask_name': 'totally_different.3mf', 'total_layer_num': 999})
        assert job_status(10) == 'pending'
