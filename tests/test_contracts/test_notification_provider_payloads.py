"""Actual channel dispatch payloads with recorded transports and no external delivery."""
import json
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_notification_channel_behavior import channels_db,review_db

@pytest.fixture
def recorded_transport(channels_db,monkeypatch):
    from core import license_manager
    from modules.notifications.routes import webhooks  # Import before substituting license lookup.
    monkeypatch.setattr(license_manager,'get_license',lambda:SimpleNamespace(has_feature=lambda feature:True))
    class ImmediateThread:
        def __init__(self,target,daemon):self.target=target
        def start(self):self.target()
    monkeypatch.setattr(channels_db.threading,'Thread',ImmediateThread)
    calls=[]
    for method in ['safe_post','trusted_post']:
        monkeypatch.setattr(channels_db,method,lambda url,_method=method,**kwargs:calls.append((_method,url,kwargs)))
    return calls


def add_hook(db,kind,enabled=True,events=None):
    addresses={'ntfy':'https://notify.example.invalid/topic','telegram':'synthetic-noncredential|synthetic-chat','whatsapp':'synthetic-phone|synthetic-recipient|synthetic-noncredential','pushover':'synthetic-user|synthetic-noncredential','discord':'https://discord.example.invalid/hook','slack':'https://slack.example.invalid/hook'}
    db.execute(text('INSERT INTO webhooks(name,url,webhook_type,alert_types,is_enabled) VALUES(:name,:url,:kind,:events,:enabled)'),{'name':'Synthetic channel','url':addresses[kind],'kind':kind,'events':json.dumps(events or ['print_failed']),'enabled':enabled});db.commit()


@pytest.mark.parametrize('severity,priority',[('critical',1),('error',0),('warning',0),('info',0)])
def test_pushover_priority_and_complete_payload(channels_db,review_db,recorded_transport,severity,priority):
    add_hook(review_db,'pushover')
    channels_db.send_webhook('print_failed','Synthetic title','Synthetic details',severity)
    assert len(recorded_transport)==1
    method,url,request=recorded_transport[0]
    assert method=='trusted_post' and url=='https://api.pushover.net/1/messages.json'
    assert request['data']=={'token':'synthetic-noncredential','user':'synthetic-user','title':'Synthetic title','message':'Synthetic details','priority':priority}
    assert request['timeout']==10

@pytest.mark.parametrize('severity,priority',[('critical','urgent'),('error','high'),('warning','high'),('info','default')])
def test_ntfy_priority_title_and_body(channels_db,review_db,recorded_transport,severity,priority):
    add_hook(review_db,'ntfy')
    channels_db.send_webhook('print_failed','Synthetic title','Synthetic details',severity)
    assert len(recorded_transport)==1
    method,url,request=recorded_transport[0]
    assert method=='safe_post' and url=='https://notify.example.invalid/topic'
    assert request['headers']=={'Title':'Synthetic title','Priority':priority,'Tags':'printer'}
    assert request['content']=='Synthetic details' and request['timeout']==10

@pytest.mark.parametrize('kind',['telegram','whatsapp'])
def test_provider_routes_and_required_recipient_payload(channels_db,review_db,recorded_transport,kind):
    add_hook(review_db,kind)
    channels_db.send_webhook('print_failed','Synthetic title','Synthetic details','warning')
    assert len(recorded_transport)==1
    method,url,request=recorded_transport[0]
    assert method=='trusted_post' and request['timeout']==10
    if kind=='telegram':
        assert url=='https://api.telegram.org/botsynthetic-noncredential/sendMessage'
        assert request['json']['chat_id']=='synthetic-chat'
        assert request['json']['parse_mode']=='Markdown'
        assert 'Synthetic title' in request['json']['text'] and 'Synthetic details' in request['json']['text']
    else:
        assert url=='https://graph.facebook.com/v18.0/synthetic-phone/messages'
        assert request['headers']['Authorization']=='Bearer synthetic-noncredential'
        assert request['json']['messaging_product']=='whatsapp' and request['json']['to']=='synthetic-recipient'
        assert request['json']['type']=='text' and 'Synthetic details' in request['json']['text']['body']

@pytest.mark.parametrize('kind',['ntfy','telegram','whatsapp','pushover'])
@pytest.mark.parametrize('reason',['disabled','unmatched'])
def test_unselected_hooks_never_reach_transport(channels_db,review_db,recorded_transport,kind,reason):
    add_hook(review_db,kind,enabled=reason!='disabled',events=['other_event'] if reason=='unmatched' else None)
    channels_db.send_webhook('print_failed','Synthetic title','Synthetic details')
    assert recorded_transport==[]

@pytest.mark.parametrize('kind',['discord','slack'])
def test_discord_slack_payload_and_event_selection(channels_db,review_db,recorded_transport,kind):
    add_hook(review_db,kind)
    channels_db.send_webhook('print_failed','Synthetic title','Synthetic details','critical')
    assert len(recorded_transport)==1
    method,url,request=recorded_transport[0]
    assert method=='safe_post' and url==f'https://{kind}.example.invalid/hook' and request['timeout']==10
    if kind=='discord':
        embed=request['json']['embeds'][0]
        assert 'Synthetic title' in embed['title'] and embed['description']=='Synthetic details'
        assert embed['color']==0xef4444 and embed['footer']=={'text':'O.D.I.N.'}
    else:
        header,body=request['json']['blocks']
        assert header['text']['type']=='plain_text' and 'Synthetic title' in header['text']['text']
        assert body['text']['text']=='Synthetic details'

@pytest.mark.parametrize('invalid',['{broken','"all"','{"all":true}','123','null'])
def test_malformed_event_selection_never_expands_delivery(channels_db,review_db,recorded_transport,invalid):
    add_hook(review_db,'discord')
    review_db.execute(text('UPDATE webhooks SET alert_types=:invalid'),{'invalid':invalid});review_db.commit()
    channels_db.send_webhook('print_failed','Synthetic title','Synthetic details')
    assert recorded_transport==[]


def test_explicit_all_event_selection_remains_supported(channels_db,review_db,recorded_transport):
    add_hook(review_db,'discord',events=['all'])
    channels_db.send_webhook('print_complete','Synthetic title','Synthetic details')
    assert len(recorded_transport)==1

@pytest.mark.parametrize('path',['channel','route'])
@pytest.mark.parametrize('stored,expected',[
    (None,True),('',True),('[]',False),('["all"]',True),
    ('["print_failed"]',True),('["print_fail"]',False),
    ('{broken',False),('"all"',False),('{"all":true}',False),
    ('123',False),('null',False),('['*1200+'0'+']'*1200,False),('["all",123]',False),
    ('["all",""]',False),('["all","  "]',False),('["all",null]',False),
])
def test_both_dispatch_paths_filter_before_decryption_and_keep_valid_siblings(
    channels_db,review_db,recorded_transport,monkeypatch,path,stored,expected
):
    from modules.notifications.routes import webhooks
    monkeypatch.setattr(webhooks,'get_license',lambda:SimpleNamespace(has_feature=lambda feature:True))
    monkeypatch.setattr(webhooks,'safe_post',channels_db.safe_post)
    monkeypatch.setattr(webhooks,'trusted_post',channels_db.trusted_post)
    add_hook(review_db,'discord')
    review_db.execute(text('UPDATE webhooks SET alert_types=:value'),{'value':stored})
    review_db.commit()
    add_hook(review_db,'slack')
    decryptions=[]
    target=channels_db if path=='channel' else webhooks
    monkeypatch.setattr(target,'_decrypt_webhook_url',lambda value:decryptions.append(value) or value)
    if path=='channel':target.send_webhook('print_failed','Synthetic','Synthetic')
    else:target._dispatch_to_webhooks(review_db,'print_failed','Synthetic','Synthetic','info')
    assert len(recorded_transport)==1+int(expected)
    assert len(decryptions)==1+int(expected)
    assert any(call[1]=='https://slack.example.invalid/hook' for call in recorded_transport)
