"""Execute digest driver claims, outcomes and safe formatting with isolated data."""
from datetime import datetime,timezone
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

END=datetime(2026,10,10,7,tzinfo=timezone.utc)


@pytest.fixture
def digest_setup(review_db,monkeypatch):
    from modules.notifications import quiet_hours
    from modules.reporting import report_runner
    target={'id':1,'name':'Synthetic school','config':{'enabled':True,'start':'22:00','end':'07:00'}}
    alerts=[{'user_id':3,'printer_id':9,'alert_type':'print_complete','severity':'info','title':'Synthetic finished','created_at':END.isoformat()}]
    monkeypatch.setattr(quiet_hours,'iter_orgs_with_digest_enabled',lambda:[target])
    monkeypatch.setattr(quiet_hours,'compute_last_window_end',lambda config:END)
    monkeypatch.setattr(quiet_hours,'get_suppressed_alerts_for_window',lambda *a,**k:alerts)
    monkeypatch.setattr(report_runner,'get_smtp_config',lambda session:{'server':'synthetic.invalid'})
    return report_runner


@pytest.mark.parametrize('channel',['email','push'])
def test_failed_delivery_retains_failed_claim_and_does_not_repeat(channel,digest_setup,review_db,monkeypatch):
    runner=digest_setup
    review_db.execute(text('INSERT INTO alert_preferences(user_id,alert_type,email,browser_push) VALUES(3,\'print_complete\',:email,:push)'),
        {'email':int(channel=='email'),'push':int(channel=='push')});review_db.commit()
    calls=[]
    monkeypatch.setattr(runner,'_deliver_digest_'+channel,lambda *a,**k:calls.append(channel) or False)
    runner.process_quiet_hours_digest(review_db)
    with Session(review_db.get_bind()) as fresh:
        row=fresh.execute(text('SELECT delivery_status FROM quiet_hours_digest_sends WHERE user_id=3')).scalar_one()
        assert row.startswith('failed:'),row
        runner.process_quiet_hours_digest(fresh)
    assert calls==[channel]


def test_digest_html_escapes_alert_fields():
    from modules.notifications.quiet_hours import format_digest_html
    rendered=format_digest_html([{'severity':'<img src=x>','title':'<script>synthetic</script>',
        'created_at':'<svg onload=x>'}])
    assert '<script>' not in rendered and '<img src=x>' not in rendered and '<svg onload=x>' not in rendered
    assert '&lt;script&gt;synthetic&lt;/script&gt;' in rendered


@pytest.mark.parametrize('hour,minute,expected',[(6,59,None),(7,0,7),(12,0,7),(22,0,None)])
def test_digest_window_uses_explicit_clock(hour,minute,expected,monkeypatch):
    from modules.notifications import quiet_hours
    class WallClock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,10,10,23,tzinfo=timezone.utc)
    monkeypatch.setattr(quiet_hours,'datetime',WallClock)
    now=datetime(2026,10,10,hour,minute,tzinfo=timezone.utc)
    result=quiet_hours.compute_last_window_end({'enabled':True,'start':'22:00','end':'07:00'},now=now)
    assert result==(None if expected is None else now.replace(hour=expected,minute=0))


def test_digest_claims_distinguish_scopes_and_global_repeat(review_db):
    from modules.reporting.report_runner import _claim_digest_send,_claim_org_webhook_send
    assert _claim_digest_send(review_db,3,1,END) is True
    assert _claim_digest_send(review_db,3,1,END) is False
    assert _claim_digest_send(review_db,3,2,END) is True
    assert _claim_digest_send(review_db,3,None,END) is True
    assert _claim_org_webhook_send(review_db,None,END) is True
    assert _claim_org_webhook_send(review_db,None,END) is False


def test_global_webhook_claim_is_unique_even_without_org(review_db):
    from modules.reporting.report_runner import _claim_org_webhook_send
    assert _claim_org_webhook_send(review_db,None,END) is True
    assert _claim_org_webhook_send(review_db,None,END) is False


def test_digest_resource_scope_and_half_open_boundaries(review_db,monkeypatch):
    from modules.notifications import quiet_hours
    from datetime import timedelta
    start=END-timedelta(hours=9)
    review_db.execute(text("INSERT INTO printers(id,name,org_id,is_active) VALUES(11,'Foreign',2,1),(12,'Global',NULL,1)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,charged_to_org_id,status) VALUES(201,'Own',1,'completed'),(202,'Foreign',2,'completed')"))
    review_db.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(201,'Synthetic','PLA')"))
    review_db.execute(text('INSERT INTO spools(id,filament_id,org_id) VALUES(201,201,1)'))
    review_db.execute(text('UPDATE users SET is_active=0 WHERE id=4'))
    # Orgs 1 and 2 have their own quiet hours, so their alerts belong to their own digests;
    # an org without them would be governed (and digested) by the system scope.
    review_db.execute(text('''UPDATE groups SET settings_json='{"quiet_hours_enabled": true}' WHERE id IN (1,2)'''))
    cases=[(1,3,None,201,None,start+timedelta(minutes=1)),
        (2,3,None,None,201,start+timedelta(minutes=2)),(3,3,11,None,None,start),
        (4,3,12,None,None,start),
        (6,3,9,202,None,start),(7,4,9,None,None,start),(8,3,9,None,None,END),
        (9,3,9,None,None,start-timedelta(seconds=1)),(10,3,None,201,None,start),
        (11,5,11,None,None,start)]
    for id,user,printer,job,spool,created in cases:
        review_db.execute(text("INSERT INTO alerts(id,user_id,alert_type,severity,title,printer_id,job_id,spool_id,created_at) VALUES(:id,:user,'print_complete','info','Synthetic',:printer,:job,:spool,:created)"),
            {'id':id,'user':user,'printer':printer,'job':job,'spool':spool,'created':created.isoformat()})
    review_db.commit()
    # Simulate a historical damaged reference in this disposable database.
    raw=review_db.get_bind().raw_connection()
    try:
        raw.execute('PRAGMA foreign_keys=OFF')
        raw.execute("INSERT INTO alerts(id,user_id,alert_type,severity,title,job_id,created_at) VALUES(5,3,'print_complete','info','Legacy missing resource',9999,?)",(start.isoformat(),))
        raw.commit()
        raw.execute('PRAGMA foreign_keys=ON')
    finally:raw.close()
    monkeypatch.setattr(quiet_hours,'engine',review_db.get_bind())
    assert {r['id'] for r in quiet_hours.get_suppressed_alerts_for_window(start,END,1)}=={1,2,10}
    assert {r['id'] for r in quiet_hours.get_suppressed_alerts_for_window(start,END,None)}=={4}
    assert {r['id'] for r in quiet_hours.get_suppressed_alerts_for_window(start,END,2)}=={11}


def test_competing_workers_claim_once_and_restart_retains_claim(review_db,digest_setup,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    runner=digest_setup
    barrier=threading.Barrier(4)
    def worker(_):
        with Session(review_db.get_bind()) as session:
            barrier.wait(timeout=3)
            return runner._claim_digest_send(session,3,1,END)
    with ThreadPoolExecutor(max_workers=4) as pool:outcomes=list(pool.map(worker,range(4)))
    assert outcomes.count(True)==1 and outcomes.count(False)==3
    calls=[]
    monkeypatch.setattr(runner,'_deliver_digest_email',lambda *a,**k:calls.append('email') or True)
    monkeypatch.setattr(runner,'_deliver_digest_push',lambda *a,**k:calls.append('push') or True)
    with Session(review_db.get_bind()) as restarted:
        runner.process_quiet_hours_digest(restarted)
        assert restarted.execute(text('SELECT delivery_status FROM quiet_hours_digest_sends')).scalar_one()=='pending'
    assert calls==[]


@pytest.mark.parametrize('status',['sent','pending','failed:email'])
def test_legacy_claim_prevents_all_scopes_after_upgrade(status,review_db):
    from modules.reporting import report_runner as runner
    review_db.execute(text("INSERT INTO quiet_hours_digest_sends(user_id,org_id,scope_key,window_ended_at,delivery_status) VALUES(3,1,'legacy',:end,:status)"),{'end':END.isoformat(),'status':status})
    review_db.execute(text("INSERT INTO quiet_hours_org_digest_sends(org_id,scope_key,window_ended_at,delivery_status) VALUES(NULL,'legacy:1',:end,:status)"),{'end':END.isoformat(),'status':status});review_db.commit()
    for scope in [1,2,None]:assert runner._claim_digest_send(review_db,3,scope,END) is False
    assert runner._claim_org_webhook_send(review_db,None,END) is False
    assert review_db.execute(text('SELECT count(*) FROM quiet_hours_digest_sends')).scalar_one()==1


@pytest.mark.parametrize('outcome',[None,False,'exception'])
def test_digest_non_success_outcomes_are_failed(outcome,digest_setup,review_db,monkeypatch):
    runner=digest_setup
    review_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,email) VALUES(3,'print_complete',1)"));review_db.commit()
    calls=[]
    def deliver(*args):
        calls.append(1)
        if outcome=='exception':raise RuntimeError('Synthetic rejection')
        return outcome
    monkeypatch.setattr(runner,'_deliver_digest_email',deliver)
    runner.process_quiet_hours_digest(review_db);runner.process_quiet_hours_digest(review_db)
    assert review_db.execute(text('SELECT delivery_status FROM quiet_hours_digest_sends')).scalar_one().startswith('failed:')
    assert calls==[1]


@pytest.mark.parametrize('problem',['no_preferences','no_smtp','inactive','revoked'])
def test_digest_unavailable_or_unauthorized_never_calls_channel(problem,digest_setup,review_db,monkeypatch):
    runner=digest_setup
    if problem!='no_preferences':review_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,email) VALUES(3,'print_complete',1)"))
    if problem=='no_smtp':monkeypatch.setattr(runner,'get_smtp_config',lambda _:None)
    elif problem=='inactive':review_db.execute(text('UPDATE users SET is_active=0 WHERE id=3'))
    elif problem=='revoked':review_db.execute(text('UPDATE users SET group_id=2 WHERE id=3'))
    review_db.commit();calls=[]
    monkeypatch.setattr(runner,'_deliver_digest_email',lambda *a,**k:calls.append('forbidden') or True)
    monkeypatch.setattr(runner,'_deliver_digest_push',lambda *a,**k:calls.append('forbidden') or True)
    runner.process_quiet_hours_digest(review_db)
    assert calls==[]
    assert review_db.execute(text('SELECT delivery_status FROM quiet_hours_digest_sends')).scalar_one().startswith('failed:')


from tests.test_contracts.test_notification_channel_behavior import smtp_peer,channels_db


def test_digest_driver_actual_smtp_escapes_and_records_success(digest_setup,review_db,monkeypatch,smtp_peer):
    from email import policy
    from email.parser import BytesParser
    from modules.notifications import quiet_hours
    port,received=smtp_peer;runner=digest_setup
    monkeypatch.setattr(runner,'get_smtp_config',lambda _: {'host':'127.0.0.1','port':port,'use_tls':False,'from_address':'odin@example.test'})
    monkeypatch.setattr(quiet_hours,'get_suppressed_alerts_for_window',lambda *a,**k:[{
        'user_id':3,'printer_id':9,'alert_type':'print_complete','severity':'info',
        'title':'<script>synthetic</script>','created_at':END.isoformat()}])
    review_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,email) VALUES(3,'print_complete',1)"));review_db.commit()
    runner.process_quiet_hours_digest(review_db)
    assert len(received)==1
    message=BytesParser(policy=policy.default).parsebytes(received[0])
    assert message['To']=='admin@example.test'
    html=message.get_body(preferencelist=('html',)).get_content()
    assert '<script>' not in html and '&lt;script&gt;synthetic&lt;/script&gt;' in html
    assert review_db.execute(text('SELECT delivery_status FROM quiet_hours_digest_sends')).scalar_one()=='sent'
    runner.process_quiet_hours_digest(review_db)
    assert len(received)==1


def test_digest_driver_actual_smtp_rejection_preserves_failure(digest_setup,review_db,monkeypatch):
    import socketserver,threading
    runner=digest_setup;attempts=[]
    class Reject(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(3)
            self.wfile.write(b'220 synthetic.test\r\n')
            while True:
                line=self.rfile.readline()
                if not line:break
                verb=line.split(b' ',1)[0].strip().upper()
                if verb==b'RCPT':
                    attempts.append(1);self.wfile.write(b'550 Synthetic rejection\r\n')
                elif verb==b'QUIT':
                    self.wfile.write(b'221 bye\r\n');break
                else:self.wfile.write(b'250 OK\r\n')
    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),Reject)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    monkeypatch.setattr(runner,'get_smtp_config',lambda _: {'host':'127.0.0.1','port':server.server_address[1],'use_tls':False})
    review_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,email) VALUES(3,'print_complete',1)"));review_db.commit()
    try:
        runner.process_quiet_hours_digest(review_db)
        runner.process_quiet_hours_digest(review_db)
    finally:server.shutdown();server.server_close();worker.join(timeout=2)
    assert attempts==[1]
    assert review_db.execute(text('SELECT delivery_status FROM quiet_hours_digest_sends')).scalar_one()=='failed:email:rejected'


@pytest.mark.parametrize('problem',['none','missing_vapid','partial','itar','success'])
def test_webpush_truthful_aggregate_and_digest_propagation(problem,channels_db,review_db,monkeypatch):
    import json,requests,pywebpush
    from modules.reporting import report_runner
    if problem!='none':
        review_db.execute(text("INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key) VALUES(3,'https://push.example.test/one','synthetic','synthetic'),(3,'https://push.example.test/two','synthetic','synthetic')"))
    if problem!='missing_vapid':
        review_db.execute(text("INSERT INTO system_config(key,value) VALUES('vapid_keys',:value)"),{'value':json.dumps({'private_key':'synthetic-noncredential'})})
    review_db.commit();calls=[]
    def submit(**kwargs):
        calls.append(kwargs['subscription_info']['endpoint'])
        if problem=='partial' and len(calls)==2:
            response=requests.Response();response.status_code=503
            raise pywebpush.WebPushException('Synthetic rejected device',response=response)
    monkeypatch.setattr(pywebpush,'webpush',submit)
    if problem=='itar':monkeypatch.setenv('ODIN_ITAR_MODE','true')
    outcome=report_runner._deliver_digest_push(review_db,3,[{}],END)
    assert outcome is (problem=='success')
    assert len(calls)==(2 if problem in ('partial','success') else 0)
    assert review_db.execute(text('SELECT count(*) FROM push_subscriptions WHERE user_id=3')).scalar_one()==(0 if problem=='none' else 2)


@pytest.mark.parametrize('storage',['orm','iso_offset'])
def test_digest_query_accepts_actual_orm_and_legacy_time_formats(storage,review_db,monkeypatch):
    from datetime import timedelta
    from modules.notifications import quiet_hours
    from modules.notifications.models import Alert
    start=END-timedelta(hours=9)
    if storage=='orm':
        review_db.add(Alert(user_id=3,alert_type='print_complete',severity='info',title='Actual ORM time',printer_id=9,created_at=start+timedelta(minutes=30)))
    else:
        stamp=(END-timedelta(minutes=5)).astimezone(timezone(timedelta(hours=2))).isoformat()
        review_db.execute(text("INSERT INTO alerts(user_id,alert_type,severity,title,printer_id,created_at) VALUES(3,'print_complete','info','Legacy offset',9,:stamp)"),{'stamp':stamp})
    review_db.commit()
    monkeypatch.setattr(quiet_hours,'engine',review_db.get_bind())
    rows=quiet_hours.get_suppressed_alerts_for_window(start,END,1)
    assert len(rows)==1 and rows[0]['user_id']==3


def test_digest_preferences_filter_each_channel_by_alert_type(digest_setup,review_db,monkeypatch):
    from modules.notifications import quiet_hours
    runner=digest_setup
    alerts=[{'user_id':3,'printer_id':9,'alert_type':kind,'title':kind} for kind in ('print_complete','print_failed','low_stock')]
    monkeypatch.setattr(quiet_hours,'get_suppressed_alerts_for_window',lambda *a,**k:alerts)
    review_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,email,browser_push) VALUES(3,'print_complete',1,0),(3,'print_failed',0,1),(3,'low_stock',0,0)"));review_db.commit()
    delivered={}
    def email(session,config,user,rows,end):delivered['email']=[r['alert_type'] for r in rows];return True
    def push(session,user,rows,end):delivered['push']=[r['alert_type'] for r in rows];return True
    monkeypatch.setattr(runner,'_deliver_digest_email',email);monkeypatch.setattr(runner,'_deliver_digest_push',push)
    runner.process_quiet_hours_digest(review_db)
    assert delivered=={'email':['print_complete'],'push':['print_failed']}
    assert review_db.execute(text('SELECT delivery_status FROM quiet_hours_digest_sends')).scalar_one()=='sent'


@pytest.mark.parametrize('deferral_table',['absent','registered'])
def test_global_and_tenant_webhooks_do_not_mix_content(deferral_table,review_db,monkeypatch):
    """absent: a database not yet upgraded by 032 (webhook digests come from deduplicated in-app
    rows). registered: 032's per-alert webhook rows are authoritative for webhook digests."""
    from modules.notifications import quiet_hours
    from modules.notifications import deferred_deliveries
    from modules.reporting import report_runner as runner
    from datetime import timedelta
    start=END-timedelta(hours=9)
    if deferral_table=='absent':
        review_db.execute(text("DROP TABLE quiet_hours_deferred_deliveries"))
    review_db.execute(text("INSERT INTO printers(id,name,org_id) VALUES(12,'Global',NULL)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,charged_to_org_id) VALUES(201,'Tenant job',1)"))
    for title,printer,job,owner,scope in [('Tenant secret',None,201,1,'org:1'),('Global state',12,None,None,'system')]:
        review_db.execute(text("INSERT INTO alerts(user_id,alert_type,severity,title,printer_id,job_id,created_at) VALUES(3,'print_complete','info',:title,:printer,:job,:stamp)"),{'title':title,'printer':printer,'job':job,'stamp':(start+timedelta(hours=1)).isoformat()})
        if deferral_table=='registered':
            assert deferred_deliveries.record_suppressed(review_db,org_id=owner,scope=scope,recipients={},alert_type='print_complete',
                                                         severity='info',title=title,printer_id=printer,job_id=job)==1
    if deferral_table=='registered':
        review_db.execute(text("UPDATE quiet_hours_deferred_deliveries SET created_at=:stamp"),{'stamp':(start+timedelta(hours=1)).replace(tzinfo=None).isoformat(sep=' ')})
    review_db.commit()
    monkeypatch.setattr(quiet_hours,'engine',review_db.get_bind())
    config={'enabled':True,'start':'22:00','end':'07:00','webhook_url':'https://synthetic.example.test/digest'}
    monkeypatch.setattr(quiet_hours,'iter_orgs_with_digest_enabled',lambda:[{'id':1,'name':'Tenant','config':config},{'id':None,'name':'Global','config':config}])
    monkeypatch.setattr(quiet_hours,'compute_last_window_end',lambda _:END)
    monkeypatch.setattr(runner,'get_smtp_config',lambda _:None)
    deliveries=[]
    def capture(url,kind,alerts,name,end):deliveries.append((name,[a['title'] for a in alerts]));return True
    monkeypatch.setattr(runner,'_deliver_digest_webhook',capture)
    runner.process_quiet_hours_digest(review_db);runner.process_quiet_hours_digest(review_db)
    assert deliveries==[('Tenant',['Tenant secret']),('Global',['Global state'])]
    assert {r[0] for r in review_db.execute(text('SELECT scope_key FROM quiet_hours_digest_sends'))}=={'org:1','system'}
