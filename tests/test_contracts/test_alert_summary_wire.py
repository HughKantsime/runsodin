"""Signed authenticated alert summary contract, isolated synthetic records."""
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


def test_alert_summary_counts_only_current_principal_active_categories(token_client, review_db):
    from core.auth import create_access_token
    from modules.notifications.routes import alerts
    client, _ = token_client
    client.app.include_router(alerts.router)
    review_db.execute(text("DELETE FROM alerts"))
    for user, kind, read, dismissed in [(3,'print_failed',False,False),(3,'spool_low',False,False),(3,'maintenance_overdue',False,False),(3,'print_failed',True,False),(3,'print_failed',False,True),(1,'print_failed',False,False)]:
        review_db.execute(text("INSERT INTO alerts(user_id,alert_type,severity,title,is_read,is_dismissed) VALUES(:user,:kind,'warning','Synthetic owned summary',:read,:dismissed)"), dict(user=user,kind=kind,read=read,dismissed=dismissed))
    review_db.commit()
    response = client.get('/alerts/summary', headers={'Authorization':'Bearer '+create_access_token(data={'sub':'admin'})})
    assert response.status_code == 200
    assert response.json() == {'print_failed':1,'spool_low':1,'maintenance_overdue':1,'total':3}

    unread = client.get('/alerts/unread-count', headers={'Authorization':'Bearer '+create_access_token(data={'sub':'admin'})})
    assert unread.status_code == 200
    assert unread.json() == {'unread_count':4}


def test_analytics_producer_retains_zero_success_rate(token_client, review_db):
    from core.auth import create_access_token
    from modules.reporting.routes import analytics
    client, _ = token_client
    client.app.include_router(analytics.router)
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,printer_id,charged_to_org_id,created_at) VALUES('Synthetic failed printer','failed',1,9,1,CURRENT_TIMESTAMP)"))
    review_db.commit()
    response = client.get('/analytics', headers={'Authorization':'Bearer '+create_access_token(data={'sub':'admin'})})
    assert response.status_code == 200
    row = next(row for row in response.json()['printer_stats'] if row['id']==9)
    assert row['success_rate']==0 and row['failed_jobs']==1


def test_analytics_daily_money_is_unknown_not_zero_for_unpriced_jobs(token_client, review_db):
    from core.auth import create_access_token
    from modules.reporting.routes import analytics
    client, _ = token_client
    client.app.include_router(analytics.router)
    review_db.execute(text("INSERT INTO jobs(item_name,status,quantity,charged_to_org_id,created_at) VALUES('Synthetic daily activity','completed',1,1,CURRENT_TIMESTAMP)"))
    review_db.commit()
    response = client.get('/analytics', headers={'Authorization':'Bearer '+create_access_token(data={'sub':'admin'})})
    assert response.status_code == 200
    daily = response.json()['jobs_by_date']
    assert daily
    assert all(set(row)=={'created','completed','revenue','cost'} for row in daily.values())
    # The completed job has no price, model or cost: its day's money is unknown, never a fabricated 0.
    priced_days = [row for row in daily.values() if row['completed']]
    assert priced_days and all(row['revenue'] is None and row['cost'] is None for row in priced_days)
