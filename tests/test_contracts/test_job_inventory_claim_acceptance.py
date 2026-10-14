"""Owned advisory API and scheduler scoring claim assertions; no device I/O."""
from sqlalchemy import text
from tests.test_contracts.test_job_approval_notification_behavior import approval_client
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_license_user_limits import signed_license

def test_color_score_prefers_matching_loaded_material_colors():
    from modules.jobs.scheduler import Scheduler
    score = Scheduler._calculate_color_score
    assert score(None, ['#AaBbCc','#112233'], ['#aabbcc','#112233']) == 100
    assert score(None, ['#AaBbCc'], ['#aabbcc','#112233']) == 50
    assert score(None, ['#ffffff'], ['#aabbcc','#112233']) == 0
    assert score(None, [], []) == 50

def test_actual_filament_check_reports_type_and_color_mismatch(approval_client, review_db):
    client, _ = approval_client
    review_db.execute(text("UPDATE filament_slots SET filament_type='PLA',color='#112233' WHERE printer_id=9")); review_db.commit()
    response=client.get('/api/v1/jobs/filament-check',params={'printer_id':9,'filament_type':'PETG','colors':'#abcdef'})
    assert response.status_code==200
    assert len(response.json()['filament_warnings'])==2
    response=client.get('/api/v1/jobs/filament-check',params={'printer_id':9,'filament_type':'PLA','colors':'#112233'})
    assert response.status_code==200 and response.json()['filament_warnings']==[]

def test_filament_check_refuses_foreign_private_printer(approval_client, review_db):
    client,_=approval_client
    review_db.execute(text("INSERT INTO printers(id,name,api_type,org_id,shared) VALUES(991,'Owned foreign fixture','bambu',2,0)")); review_db.commit()
    assert client.get('/api/v1/jobs/filament-check',params={'printer_id':991}).status_code==404
