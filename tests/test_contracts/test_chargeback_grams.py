"""Chargeback report includes measured grams per user (F10-10.7-02)."""
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


def test_chargeback_reports_measured_grams_without_inflating_job_counts(token_client, review_db):
    from modules.reporting.routes import reports
    client, issue = token_client
    client.app.include_router(reports.router)
    review_db.execute(text('UPDATE jobs SET charged_to_user_id=NULL'))
    for identifier, cost in [(901, 2.0), (902, 3.0), (903, 1.0)]:
        review_db.execute(text("INSERT INTO jobs(id,item_name,charged_to_user_id,charged_to_org_id,estimated_cost,duration_hours,created_at) "
                               "VALUES(:id,'Grams sample',1,1,:cost,1.0,'2026-10-12 12:00:00')"), {'id': identifier, 'cost': cost})
    # Job 901: failed attempt (40 g) + successful reprint (55.5 g). Job 902: one unmeasured attempt. Job 903: never printed.
    for job, grams, status in [(901, 40.0, 'failed'), (901, 55.5, 'completed'), (902, None, 'completed')]:
        review_db.execute(text("INSERT INTO print_archives(job_id,print_name,status,filament_used_grams) VALUES(:j,'Grams sample',:s,:g)"),
                          {'j': job, 's': status, 'g': grams})
    review_db.commit()
    response = client.get('/reports/chargebacks', headers=issue(['read:analytics']), params={'start_date': '2026-10-12', 'end_date': '2026-10-12'})
    assert response.status_code == 200, response.text
    assert response.json() == [{'user_id': 1, 'username': 'student', 'job_count': 3, 'total_cost': 6.0, 'total_hours': 3.0,
                                'total_grams': 95.5, 'unmeasured_prints': 1}]


def test_scheduled_chargeback_summary_renders_grams(review_db):
    from modules.reporting.report_runner import generate_chargeback_summary
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    review_db.execute(text('UPDATE jobs SET charged_to_user_id=NULL'))
    review_db.execute(text("INSERT INTO jobs(id,item_name,charged_to_user_id,estimated_cost,duration_hours,created_at) VALUES(911,'Grams sample',1,1,1,:now)"), {'now': now})
    review_db.execute(text("INSERT INTO print_archives(job_id,print_name,status,filament_used_grams) VALUES(911,'Grams sample','completed',12.4)"))
    review_db.commit()
    html = generate_chargeback_summary(review_db, {})
    assert 'GRAMS' in html and '>12g<' in html
