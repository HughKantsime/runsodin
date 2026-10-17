"""Real schedule polling with an isolated database and substituted mail transport."""
import json
import pytest

# Report and digest emails are Pro (email_notifications; review O6).
pytestmark = pytest.mark.usefixtures("pro_license")
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db


def seed(db, *, active=True, future=False):
    db.execute(text("INSERT INTO report_schedules(id,name,report_type,frequency,recipients,filters,is_active,next_run_at) VALUES(199,'Synthetic scheduled report','job_summary','daily',:recipients,'{}',:active,:due)"), {'recipients':json.dumps(['synthetic@example.test']),'active':active,'due':'2999-01-01T08:00:00+00:00' if future else '2000-01-01T08:00:00+00:00'})
    db.commit()


@pytest.mark.parametrize('active,future,configured,expected',[(True,False,True,1),(False,False,True,0),(True,True,True,0),(True,False,False,0)])
def test_polling_only_processes_due_active_configured_reports(review_db,monkeypatch,active,future,configured,expected):
    from modules.reporting import report_runner as runner
    seed(review_db,active=active,future=future)
    monkeypatch.setattr(runner,'get_smtp_config',lambda session:{'synthetic':True} if configured else None)
    calls=[]
    monkeypatch.setitem(runner.GENERATORS,'job_summary',lambda session,filters:'<p>Synthetic generated report</p>')
    monkeypatch.setattr(runner,'send_report_email',lambda config,recipient,subject,html:calls.append((recipient,html)) or True)
    runner.process_due_reports(review_db)
    assert len(calls)==expected
    row=review_db.execute(text('SELECT last_run_at,next_run_at FROM report_schedules WHERE id=199')).one()
    if expected:
        assert calls==[('synthetic@example.test','<p>Synthetic generated report</p>')]
        assert row[0] is not None and str(row[1])>'2000'
        runner.process_due_reports(review_db)
        assert len(calls)==1
    else:assert row[0] is None


def test_generator_failure_does_not_log_private_exception_text(review_db,monkeypatch,caplog):
    from modules.reporting import report_runner as runner
    seed(review_db)
    monkeypatch.setattr(runner,'get_smtp_config',lambda session:{'synthetic':True})
    marker='SYNTHETIC_PRIVATE_ERROR_CONTENT'
    def fail(session,filters):raise ValueError(marker)
    monkeypatch.setitem(runner.GENERATORS,'job_summary',fail)
    monkeypatch.setattr(runner,'send_report_email',lambda *a:pytest.fail('failed generation must never send'))
    runner.process_due_reports(review_db)
    assert marker not in caplog.text
    assert "Synthetic scheduled report" not in caplog.text
    assert "199" in caplog.text and "ValueError" in caplog.text
