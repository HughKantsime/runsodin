"""Authenticated current print-history producer, synthetic owned records."""
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


def test_print_history_exposes_completed_and_ended_at(token_client, review_db):
    from core.auth import create_access_token
    from modules.jobs.routes import presets
    client, _ = token_client
    client.app.include_router(presets.router)
    identifier = review_db.execute(text("INSERT INTO print_jobs(printer_id,status,started_at,ended_at,created_at) VALUES(9,'completed','2026-10-11T10:00:00','2026-10-11T12:00:00','2026-10-10T10:00:00') RETURNING id")).scalar_one()
    review_db.commit()
    response = client.get('/print-jobs', headers={'Authorization':'Bearer '+create_access_token(data={'sub':'admin'})})
    assert response.status_code == 200
    row = next(row for row in response.json() if row['id']==identifier)
    assert row['status']=='completed'
    assert row['ended_at']=='2026-10-11T12:00:00'
    assert row['created_at']=='2026-10-10T10:00:00'
    assert row['duration_minutes']==120
    assert 'completed_at' not in row
