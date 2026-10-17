"""Isolated HTTP schedule records and delivery outcomes, no real email."""
import pytest

# Report and digest emails are Pro (email_notifications; review O6).
pytestmark = pytest.mark.usefixtures("pro_license")
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client

@pytest.fixture
def report_client(bom_client):
    from modules.reporting.routes import reports
    from core.dependencies import get_current_user
    bom_client.app.include_router(reports.router)
    identity={'id':3,'role':'admin','group_id':None,'is_active':True}
    bom_client.app.dependency_overrides[get_current_user]=lambda:identity
    return bom_client,identity


def create_schedule(client):
    response=client.post('/report-schedules',json={'name':'Synthetic report','report_type':'job_summary','frequency':'daily','recipients':['synthetic@example.test']})
    assert response.status_code==200
    return response.json()['id']


def test_report_schedule_record_lifecycle_and_access(report_client):
    client,identity=report_client
    identifier=create_schedule(client)
    row=next(r for r in client.get('/report-schedules').json() if r['id']==identifier)
    assert row['name']=='Synthetic report' and row['frequency']=='daily' and row['recipients']==['synthetic@example.test'] and row['is_active']
    assert client.patch(f'/report-schedules/{identifier}',json={'is_active':False,'frequency':'weekly','recipients':['replacement@example.test']}).status_code==200
    row=next(r for r in client.get('/report-schedules').json() if r['id']==identifier)
    assert not row['is_active'] and row['frequency']=='weekly' and row['recipients']==['replacement@example.test']
    identity['group_id']=1
    assert client.delete(f'/report-schedules/{identifier}').status_code==403
    identity['group_id']=None
    assert client.delete(f'/report-schedules/{identifier}').status_code==200
    assert all(r['id']!=identifier for r in client.get('/report-schedules').json())


@pytest.mark.parametrize('outcome',[True,False])
def test_run_now_reports_actual_delivery_outcome(report_client,review_db,monkeypatch,outcome):
    from modules.reporting import report_runner
    client,_=report_client
    identifier=create_schedule(client)
    monkeypatch.setattr(report_runner,'SessionLocal',lambda:Session(review_db.get_bind()))
    monkeypatch.setattr(report_runner,'get_smtp_config',lambda session:{'synthetic':True})
    monkeypatch.setitem(report_runner.GENERATORS,'job_summary',lambda session,filters:'<p>Synthetic report</p>')
    sent=[]
    def deliver(config,recipient,subject,html):
        sent.append(recipient);return outcome
    monkeypatch.setattr(report_runner,'send_report_email',deliver)
    response=client.post(f'/report-schedules/{identifier}/run')
    assert sent==['synthetic@example.test']
    if outcome:assert response.status_code==200 and response.json()['status']=='sent'
    else:assert response.status_code>=400 and response.json().get('status')!='sent'


@pytest.mark.parametrize('patch',[{'frequency':'never'},{'name':''},{'recipients':[]}])
def test_report_schedule_patch_rejects_invalid_configuration_atomically(report_client,patch):
    client,_=report_client
    identifier=create_schedule(client)
    before=next(r for r in client.get('/report-schedules').json() if r['id']==identifier)
    response=client.patch(f'/report-schedules/{identifier}',json=patch)
    assert response.status_code>=400
    after=next(r for r in client.get('/report-schedules').json() if r['id']==identifier)
    assert after==before



def test_partial_run_now_delivery_reports_safe_counts(report_client,review_db,monkeypatch):
    from modules.reporting import report_runner
    client,_=report_client;identifier=create_schedule(client)
    assert client.patch(f'/report-schedules/{identifier}',json={'recipients':['first@example.test','second@example.test']}).status_code==200
    monkeypatch.setattr(report_runner,'SessionLocal',lambda:Session(review_db.get_bind()))
    monkeypatch.setattr(report_runner,'get_smtp_config',lambda session:{'synthetic':True})
    monkeypatch.setitem(report_runner.GENERATORS,'job_summary',lambda session,filters:'<p>Synthetic report</p>')
    delivered=[]
    monkeypatch.setattr(report_runner,'send_report_email',lambda config,recipient,subject,html:delivered.append(recipient) or recipient=='first@example.test')
    response=client.post(f'/report-schedules/{identifier}/run')
    assert response.status_code==502
    assert response.json()['detail']['sent_count']==1 and response.json()['detail']['failed_count']==1
    assert delivered==['first@example.test','second@example.test']
    assert 'example.test' not in response.text


@pytest.mark.parametrize('outcome',['accepted','refused','exception'])
def test_report_smtp_outcome_cleanup_and_safe_logging(monkeypatch,caplog,outcome):
    from modules.reporting import report_runner
    from core import itar
    monkeypatch.setattr(itar,'enforce_host_destination',lambda *a,**k:None)
    events=[]
    class Peer:
        def __init__(self,*a,**k):events.append('opened')
        def __enter__(self):return self
        def __exit__(self,*a):events.append('closed')
        def starttls(self):events.append('tls')
        def send_message(self,msg,*,to_addrs):
            assert to_addrs==['synthetic@example.test']
            assert msg['To']=='synthetic@example.test'
            if outcome=='exception':raise RuntimeError('synthetic-private-marker')
            return {'synthetic@example.test':(550,b'Synthetic refused')} if outcome=='refused' else {}
        def quit(self):events.append('closed')
    monkeypatch.setattr(report_runner.smtplib,'SMTP',Peer)
    result=report_runner.send_report_email({'host':'synthetic.invalid','use_tls':True},'synthetic@example.test','Synthetic subject','<p>Synthetic body</p>')
    assert result is (outcome=='accepted')
    assert events==['opened','tls','closed']
    assert 'synthetic-private-marker' not in caplog.text


@pytest.mark.parametrize('address',['bad@','first@example.test, second@example.test','not-an-address','first@example.test\r\nBcc:other@example.test'])
def test_report_recipients_require_one_mailbox_per_entry(report_client,address):
    client,_=report_client
    identifier=create_schedule(client)
    response=client.patch(f'/report-schedules/{identifier}',json={'recipients':[address]})
    assert response.status_code==400
    row=next(r for r in client.get('/report-schedules').json() if r['id']==identifier)
    assert row['recipients']==['synthetic@example.test']


def test_report_creation_honors_inactive_state(report_client):
    client,_=report_client
    response=client.post('/report-schedules',json={'name':'Synthetic paused','report_type':'job_summary','frequency':'daily','recipients':['synthetic@example.test'],'is_active':False})
    assert response.status_code==200
    row=next(r for r in client.get('/report-schedules').json() if r['id']==response.json()['id'])
    assert row['is_active'] is False
