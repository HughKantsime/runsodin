"""Sparse file source IDs remain attached to their own grams and spool."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_archive_persistence_accounting_behavior import archive_case
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('shape', ['dense', 'sparse', 'reordered', 'shared', 'missing_grams', 'unmapped'])
def test_completion_charges_each_mapped_trays_frozen_spool(archive_case, shape):
    from modules.archives.attempt_accounting import capture_attempt_spools
    archive, engine = archive_case
    materials = [{'slot': 1, 'used_grams': 20}, {'slot': 2, 'used_grams': 60}]
    mapping = [1, 0]
    if shape in ('sparse', 'reordered'):
        materials = [{'slot': 2, 'used_grams': 20}, {'slot': 4, 'used_grams': 60}]
        mapping = [-1, 1, -1, 0]
        if shape == 'reordered':
            materials.reverse()
    if shape == 'shared':
        mapping = [1, 1]
    if shape == 'missing_grams':
        materials[1].pop('used_grams')
    if shape == 'unmapped':
        mapping = [1, -1]
    with engine.begin() as conn:
        conn.execute(text('UPDATE jobs SET ams_mapping=:mapping WHERE id=7099'), {'mapping': json.dumps(mapping)})
        conn.execute(text('UPDATE print_files SET filaments_json=:materials WHERE id=7099'), {'materials': json.dumps(materials)})
        conn.execute(text("UPDATE print_jobs SET status='running',spool_snapshot=NULL WHERE id=7099"))
        assert capture_attempt_spools(conn, 7099, 9, source='multi-source-contract')
        snapshot = json.loads(conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=7099')).scalar_one())
        if shape not in ('unmapped', 'missing_grams'):
            assert [(a['source_slot'], a['estimated_grams']) for a in snapshot['material_plan']['allocations']] == (
                [(1, 20), (3, 60)] if shape in ('sparse', 'reordered') else [(0, 20), (1, 60)])
        # Later file edits cannot redirect or redistribute completion consumption.
        conn.execute(text("UPDATE print_files SET filaments_json='[]' WHERE id=7099"))
        conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7099"))
    assert archive.create_print_archive(7099, 9, True) is not None
    assert archive.create_print_archive(7099, 9, True) is not None
    with engine.connect() as conn:
        weights = conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (7099,7100) ORDER BY id')).scalars().all()
        expected = [1000, 1000] if shape in ('missing_grams', 'unmapped') else [1000, 920] if shape == 'shared' else [940, 980]
        assert weights == expected
        amounts = conn.execute(text('SELECT spool_id,weight_used_g FROM spool_usage WHERE print_job_id=7099 ORDER BY spool_id')).all()
        assert amounts == ([] if shape in ('missing_grams', 'unmapped') else [(7100, 80)] if shape == 'shared' else [(7099, 60), (7100, 20)])


@pytest.mark.parametrize('grams_available', [True, False, 'partial', 'invalid', 'absent'])
def test_operator_completion_prefers_selected_file_ids_over_model_positions(review_db, monkeypatch, grams_available):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from core import db as core_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    engine = review_db.get_bind()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO models(id,name,org_id,color_requirements) VALUES(101,'fixture',1,:requirements)"),
                     {'requirements': json.dumps({'slot1': {'grams': 999}})})
        conn.execute(text("INSERT INTO print_files(id,filename,org_id,filaments_json) VALUES(101,'fixture.3mf',1,:materials)"),
                     {'materials': json.dumps([{'slot': 4, 'used_grams': 60}, {'slot': 2, 'used_grams': 20}] if grams_available is True else [{'slot': 4, 'used_grams': 60}] if grams_available == 'partial' else {} if grams_available == 'invalid' else [] if grams_available == 'absent' else [{'slot': 4}, {'slot': 2}])})
        conn.execute(text("INSERT INTO jobs(id,item_name,model_id,printer_id,status,charged_to_org_id,quantity,hold,print_file_id,ams_mapping) VALUES(101,'fixture',101,9,'printing',1,1,0,101,'[-1,1,-1,0]')"))
        conn.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(101,'Synthetic','Test','PLA')"))
        conn.execute(text("INSERT INTO spools(id,filament_id,remaining_weight_g,initial_weight_g,org_id) VALUES(101,101,100,100,1),(102,101,100,100,1)"))
        conn.execute(text('UPDATE filament_slots SET assigned_spool_id=101 WHERE printer_id=9 AND slot_number=1'))
        conn.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,assigned_spool_id) VALUES(9,2,'PLA',102)"))
    app = FastAPI()
    app.include_router(jobs_lifecycle.router)
    def request_db():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[core_db.get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': 'operator', 'group_id': 1, 'is_active': True}
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post('/jobs/101/complete')
            assert response.status_code == 200, response.text
    with engine.connect() as conn:
        assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id IN (101,102) ORDER BY id')).scalars().all() == ([40, 80] if grams_available is True else [100, 100])
        assert conn.execute(text('SELECT count(*) FROM spool_usage WHERE job_id=101')).scalar_one() == (2 if grams_available is True else 0)
