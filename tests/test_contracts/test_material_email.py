"""Synthetic-only mail: no provider access or saved original purchase documents."""
from email.message import EmailMessage
import json
import pytest
from fastapi import HTTPException
from modules.orders.material_email import preview


def mail(body, subject='Ordered: synthetic hardware', sender='auto-confirm@amazon.com'):
    m=EmailMessage();m['From']=sender;m['Subject']=subject;m.set_content(body)
    return m.as_string()


def body(title='Synthetic magnet 100 pack', quantity='2'):
    return f'Arriving Monday\nPrivate Recipient\n123 Private Street\nOrder #\n111-1234567-1234567\nView order details\nhttps://example.invalid/private-token\n* {title}\nQuantity: {quantity}\n'


def test_multiple_orders_quantity_is_not_piece_conversion_and_no_private_body():
    result=preview(mail(body()+body('Synthetic clamps', '3').replace('111-','222-')))
    assert [c['ordered_quantity'] for c in result['candidates']]==[2,3]
    assert len({c['order_reference'] for c in result['candidates']})==2
    assert all(c['status']=='needs_mapping' for c in result['candidates'])
    assert result['stock_changed'] is False and result['purchases_changed'] is False
    assert result['source_authenticated'] is False
    output=json.dumps(result)
    for private in ('Private Recipient','Private Street','private-token','example.invalid'):
        assert private not in output
    assert 'units_per_pack' not in output and 'cost' not in output


@pytest.mark.parametrize('quantity',['0','-1','1.5','10000','two','2 pieces',''])
def test_uncertain_quantity_rejected(quantity):
    with pytest.raises(HTTPException):preview(mail(body(quantity=quantity)))


@pytest.mark.parametrize('value',[
    lambda:mail(body(),subject='Delivered: synthetic hardware'),
    lambda:mail(body(),sender='impostor@example.invalid'),
    lambda:mail(body()+body()),
    lambda:mail(body().replace('Order #\n','')),
    lambda:mail(body().replace('Quantity: 2','Private Recipient\nQuantity: 2')),
    lambda:mail(body(title='https://example.invalid/tracking')),
    lambda:mail(body(title='a'*501)),
    lambda:mail('a'*300001),
])
def test_unsupported_messages_fail_without_echo(value):
    with pytest.raises(HTTPException) as error:preview(value())
    assert 'Private' not in str(error.value.detail)


def test_multipart_prefers_plain_ignores_attached_text_and_never_fetches_html():
    m=EmailMessage();m['From']='auto-confirm@amazon.com';m['Subject']='Ordered: synthetic'
    m.set_content(body());m.add_alternative('<img src="https://example.invalid/secret">',subtype='html')
    m.add_attachment(body().encode(),maintype='text',subtype='plain',filename='extra.txt')
    assert len(preview(m.as_string())['candidates'])==1


def test_html_only_and_multiple_plain_bodies_rejected():
    m=EmailMessage();m['From']='auto-confirm@amazon.com';m['Subject']='Ordered: synthetic'
    m.set_content('<p>Order</p>',subtype='html')
    with pytest.raises(HTTPException):preview(m.as_string())
    m=EmailMessage();m['From']='auto-confirm@amazon.com';m['Subject']='Ordered: synthetic'
    m.set_content(body());m.make_mixed();extra=EmailMessage();extra.set_content(body());m.attach(extra)
    with pytest.raises(HTTPException):preview(m.as_string())


def test_forged_auth_header_does_not_prove_sender_and_identity_ignores_subject():
    raw=mail(body());raw='Authentication-Results: attacker; dkim=pass\n'+raw
    first=preview(raw)
    second=preview(mail(body(),subject='Ordered: another title count 50'))
    assert first['source_authenticated'] is False
    assert first['candidates'][0]['candidate_reference']==second['candidates'][0]['candidate_reference']


def test_preview_route_private_validation_no_stock_and_interactive_only():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from modules.orders.routes.materials import router,reader
    from core.rbac import require_interactive_session
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[reader]=lambda:{'role':'operator'}
    # Override the concrete session guard installed on this router only.
    guards=[d.call for r in app.routes if getattr(r,'path','')=='/consumables/purchases/email/preview' for d in r.dependant.dependencies]
    for guard in guards:
        if guard != reader:app.dependency_overrides[guard]=lambda:None
    client=TestClient(app)
    response=client.post('/consumables/purchases/email/preview',json={'email_text':mail(body())})
    assert response.status_code==200,response.text
    assert 'no-store' in response.headers.get('cache-control','')
    bad=client.post('/consumables/purchases/email/preview',json={'email_text':mail(body()),'secret':'SYNTHETIC_PRIVATE'})
    assert bad.status_code==422 and 'SYNTHETIC_PRIVATE' not in bad.text

@pytest.mark.parametrize('suffix',['!!!!','='])
def test_decode_time_base64_defects_are_rejected(suffix):
    import base64
    raw='From: auto-confirm@amazon.com\nSubject: Ordered: synthetic\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: base64\n\n'+base64.b64encode(body().encode()).decode()+suffix
    with pytest.raises(HTTPException):preview(raw)


def test_invalid_charset_bytes_are_not_silently_replaced():
    import base64
    raw='From: auto-confirm@amazon.com\nSubject: Ordered: synthetic\nContent-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: base64\n\n'+base64.b64encode(body().encode().replace(b'Synthetic',b'\xffSynthetic')).decode()
    with pytest.raises(HTTPException):preview(raw)


def test_unicode_surrogate_and_orphan_quantity_fail_closed():
    for raw in (mail(body())+'\ud800',mail(body()+'Quantity: 2\n')):
        with pytest.raises(HTTPException):preview(raw)


def test_deep_nested_message_returns_fixed_validation_not_server_error():
    raw='From: auto-confirm@amazon.com\nSubject: Ordered: synthetic\n'
    for i in range(1100):raw+=f'Content-Type: multipart/mixed; boundary="b{i}"\n\n--b{i}\n'
    raw+='Content-Type: text/plain\n\n'+body()
    for i in reversed(range(1100)):raw+=f'\n--b{i}--\n'
    assert len(raw.encode())<300000
    with pytest.raises(HTTPException) as error:preview(raw)
    assert error.value.status_code==422
