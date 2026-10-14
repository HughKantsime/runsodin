"""Actual persistence and recipient checks for caller-owned alert transactions."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.base import AlertType, AlertSeverity
from modules.notifications.models import AlertPreference
from tests.test_contracts.test_education_review_workflow import review_db


def test_default_seed_with_no_in_app_alert_is_committed_by_legacy_dispatch(review_db, monkeypatch):
    from modules.notifications import alert_dispatcher as dispatcher
    monkeypatch.setattr(dispatcher, 'should_suppress_notification', lambda **_: True)
    assert dispatcher.dispatch_alert(review_db, AlertType.QUEUE_ADDED, AlertSeverity.INFO,
        'Synthetic default queue', job_id=13, target_user_ids=[1]) == 0
    with Session(review_db.get_bind()) as independent:
        assert independent.execute(text('SELECT COUNT(*) FROM alert_preferences')).scalar_one() > 0
        assert independent.execute(text('SELECT COUNT(*) FROM alerts')).scalar_one() == 0


def test_staging_seed_does_not_commit_and_rolls_back(review_db, monkeypatch):
    from modules.notifications import alert_dispatcher as dispatcher
    monkeypatch.setattr(dispatcher, 'should_suppress_notification', lambda **_: True)
    plan = dispatcher.stage_alert(review_db, AlertType.PRINT_COMPLETE, AlertSeverity.INFO,
        'Synthetic staged print', job_id=13, target_user_ids=[1])
    assert plan.alerts_created == 1
    assert plan.commit_required is True
    with Session(review_db.get_bind()) as independent:
        assert independent.execute(text('SELECT COUNT(*) FROM alert_preferences')).scalar_one() == 0
        assert independent.execute(text('SELECT COUNT(*) FROM alerts')).scalar_one() == 0
    review_db.rollback()
    assert review_db.execute(text('SELECT COUNT(*) FROM alert_preferences')).scalar_one() == 0
    assert review_db.execute(text('SELECT COUNT(*) FROM alerts')).scalar_one() == 0


@pytest.mark.parametrize('change', ['inactive', 'foreign', 'resource_reassigned'])
def test_external_delivery_rechecks_recipient_and_resource_after_commit(review_db, monkeypatch, change):
    from modules.notifications import alert_dispatcher as dispatcher
    monkeypatch.setattr(dispatcher, 'should_suppress_notification', lambda **_: False)
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id) VALUES(991,'Synthetic general job','pending',1,4)"))
    review_db.add(AlertPreference(user_id=4, alert_type=AlertType.PRINT_COMPLETE,
        in_app=True, email=True, browser_push=True))
    review_db.commit()
    plan = dispatcher.stage_alert(review_db, AlertType.PRINT_COMPLETE, AlertSeverity.INFO,
        'Synthetic recheck', job_id=991, target_user_ids=[4])
    review_db.commit()
    if change == 'inactive':
        review_db.execute(text('UPDATE users SET is_active=0 WHERE id=4'))
    elif change == 'foreign':
        review_db.execute(text('UPDATE users SET group_id=2 WHERE id=4'))
    else:
        review_db.execute(text('UPDATE jobs SET charged_to_org_id=2,charged_to_user_id=NULL WHERE id=991'))
    review_db.commit()
    calls = []
    monkeypatch.setattr(dispatcher, '_deliver_email', lambda *args: calls.append('email'))
    monkeypatch.setattr(dispatcher, '_deliver_browser_push', lambda *args: calls.append('push'))
    dispatcher.deliver_staged_alert(review_db, plan)
    assert calls == []


@pytest.mark.parametrize('status,remaining', [(404,0),(410,0),(503,1)])
def test_postcommit_expired_push_cleanup_persists(review_db, monkeypatch, status, remaining):
    import pywebpush
    from modules.notifications import alert_dispatcher as dispatcher
    monkeypatch.setenv('VAPID_PRIVATE_KEY', 'synthetic-noncredential')
    monkeypatch.setattr(dispatcher, 'should_suppress_notification', lambda **_: False)
    review_db.execute(text("INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key) VALUES(1,'https://push.example.test/fixture','synthetic-noncredential','synthetic-noncredential')"))
    review_db.add(AlertPreference(user_id=1, alert_type=AlertType.PRINT_COMPLETE,
        in_app=True, email=False, browser_push=True))
    review_db.commit()
    calls = []
    def webpush(**kwargs):
        calls.append(True)
        raise RuntimeError(f'Synthetic push response {status}')
    monkeypatch.setattr(pywebpush, 'webpush', webpush)
    dispatcher.dispatch_alert(review_db, AlertType.PRINT_COMPLETE, AlertSeverity.INFO,
        'Synthetic push cleanup', job_id=13, target_user_ids=[1])
    assert calls == [True]
    with Session(review_db.get_bind()) as independent:
        assert independent.execute(text('SELECT COUNT(*) FROM push_subscriptions WHERE user_id=1')).scalar_one() == remaining


def test_push_cleanup_commit_failure_preserves_committed_job_and_alert(review_db, monkeypatch, caplog):
    import pywebpush
    from modules.notifications import alert_dispatcher as dispatcher
    monkeypatch.setenv('VAPID_PRIVATE_KEY', 'synthetic-noncredential')
    monkeypatch.setattr(dispatcher, 'should_suppress_notification', lambda **_: False)
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id) VALUES(992,'Synthetic committed job','pending',1,4)"))
    review_db.execute(text("INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key) VALUES(4,'https://push.example.test/fixture','synthetic-noncredential','synthetic-noncredential')"))
    review_db.add(AlertPreference(user_id=4, alert_type=AlertType.QUEUE_ADDED,
        in_app=True, email=True, browser_push=True))
    plan = dispatcher.stage_alert(review_db, AlertType.QUEUE_ADDED, AlertSeverity.INFO,
        'Synthetic cleanup failure', job_id=992, target_user_ids=[4])
    review_db.commit()
    def webpush(**kwargs):
        raise RuntimeError('Synthetic 410 response')
    monkeypatch.setattr(pywebpush, 'webpush', webpush)
    original = Session.commit
    def commit(db):
        if db.deleted:
            raise RuntimeError('Synthetic cleanup commit marker')
        original(db)
    monkeypatch.setattr(Session, 'commit', commit)
    emails = []
    monkeypatch.setattr(dispatcher, '_deliver_email', lambda *args: emails.append(True))
    dispatcher.deliver_staged_alert(review_db, plan)
    assert emails == [True]
    assert 'Synthetic cleanup commit marker' not in caplog.text
    with Session(review_db.get_bind()) as independent:
        assert independent.execute(text('SELECT COUNT(*) FROM jobs WHERE id=992')).scalar_one() == 1
        assert independent.execute(text('SELECT COUNT(*) FROM alerts WHERE job_id=992')).scalar_one() == 1
        assert independent.execute(text('SELECT COUNT(*) FROM push_subscriptions WHERE user_id=4')).scalar_one() == 1
