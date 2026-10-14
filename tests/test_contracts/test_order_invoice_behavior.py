"""Real HTTP PDF generation, tenant denial and untrusted filename boundaries."""
import pytest
from io import BytesIO
from pypdf import PdfReader
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client


def create_invoice_order(client, **extra):
    data={'order_number':'Synthetic invoice', 'items':[{'product_id':101,'quantity':2}]}
    data.update(extra)
    response=client.post('/orders',json=data)
    assert response.status_code==200
    return response.json()['id']


def test_invoice_missing_prices_is_still_downloadable(bom_client, review_db):
    review_db.execute(text('UPDATE products SET price=NULL WHERE id=101'))
    review_db.commit()
    identifier=create_invoice_order(bom_client)
    detail=bom_client.get(f'/orders/{identifier}').json()
    assert detail['items'][0]['subtotal'] is None
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==200
    assert response.headers['content-type']=='application/pdf'
    assert response.content.startswith(b'%PDF-')
    assert int(response.headers['content-length'])==len(response.content)


@pytest.mark.parametrize('number', ['Synthetic "quoted" invoice', 'Synthetic\r\nInjected: unsafe', 'Synthetic 客户 invoice'])
def test_invoice_download_filename_is_ascii_and_cannot_inject_headers(bom_client, number):
    identifier=create_invoice_order(bom_client,order_number=number)
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==200
    header=response.headers['content-disposition']
    assert '\r' not in header and '\n' not in header
    assert header.isascii()
    assert header==f'attachment; filename="invoice_{identifier}.pdf"'


def test_invoice_foreign_tenant_denied_before_generation(bom_client, review_db, monkeypatch):
    identifier=create_invoice_order(bom_client)
    from core.dependencies import get_current_user
    # Keep the immutable billed owner; exercise a foreign request principal.
    original = bom_client.app.dependency_overrides[get_current_user]
    principal = {**original(), 'group_id': 2}
    monkeypatch.setitem(bom_client.app.dependency_overrides,get_current_user,lambda:principal)
    assert review_db.execute(text('SELECT org_id FROM orders WHERE id=:id'),{'id':identifier}).scalar()==1
    from modules.orders.invoice_generator import InvoiceGenerator
    def forbidden(*args, **kwargs):
        raise AssertionError('Foreign invoice generation must never run')
    monkeypatch.setattr(InvoiceGenerator,'generate',forbidden)
    assert bom_client.get(f'/orders/{identifier}/invoice.pdf').status_code==404


def test_customer_invoice_does_not_disclose_internal_costs_and_profit(bom_client):
    # Recorded revenue is total charged to the customer, including charged shipping.
    identifier=create_invoice_order(bom_client,revenue=44,platform_fees=2,payment_fees=1,shipping_cost=7,shipping_charged=4)
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==200
    document=PdfReader(BytesIO(response.content))
    content='\n'.join(page.extract_text() for page in document.pages)
    assert 'Synthetic invoice' in content and 'Own product' in content
    assert '$40.00' in content and '$4.00' in content
    assert all(label not in content for label in ('Platform Fees','Payment Fees','Shipping (cost)','Cost Analysis','Estimated Cost','Actual Cost','Profit:','Total Revenue'))
    assert 'Invoice Total:' in content and '$44.00' in content


def test_free_invoice_shows_explicit_zero_total(bom_client):
    identifier=create_invoice_order(bom_client,revenue=0,shipping_charged=0,items=[{'product_id':101,'quantity':2,'unit_price':0}])
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==200
    content='\n'.join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
    assert 'Invoice Total:' in content
    assert '$0.00' in content


def test_unicode_customer_product_and_branding_are_preserved(bom_client, review_db):
    from modules.organizations.branding import get_or_create_branding
    branding=get_or_create_branding(review_db)
    branding.app_name='Synthetic 日本語 brand'
    branding.footer_text='Synthetic 한국어 footer'
    review_db.execute(text('UPDATE products SET name=:name WHERE id=101'), {'name':'Synthetic 客户 product'})
    review_db.commit()
    identifier=create_invoice_order(bom_client,customer_name='Synthetic José Ελληνικά русский')
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==200
    content='\n'.join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
    assert all(value in content for value in ['日本語','한국어','客户','José','Ελληνικά','русский'])


def test_multipage_invoice_keeps_descriptions_headings_and_page_footers(bom_client, review_db):
    name='Synthetic full product description that must remain readable beyond thirty five characters'
    review_db.execute(text('UPDATE products SET name=:name WHERE id=101'), {'name':name})
    review_db.commit()
    identifier=create_invoice_order(bom_client,revenue=4000,items=[{'product_id':101,'quantity':2,'unit_price':20} for _ in range(100)])
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==200
    document=PdfReader(BytesIO(response.content))
    assert len(document.pages)>1
    texts=[page.extract_text() for page in document.pages]
    combined=' '.join(' '.join(text.split()) for text in texts)
    assert combined.count(name)==100
    for index,content in enumerate(texts,1):
        assert f'Page {index}/{len(texts)}' in content
        coordinates=[]
        document.pages[index-1].extract_text(visitor_text=lambda text, cm, tm, font, size: coordinates.append(tm[5]) if text.strip() else None)
        assert all(0<=y<=float(document.pages[index-1].mediabox.height) for y in coordinates)
        if name[:20] in content:
            assert 'Unit Price' in content and 'Subtotal' in content
    assert combined.count('Invoice Total:')==1
    assert '$4,000.00' in texts[-1]


def test_oversize_footer_is_rejected_without_offpage_text(bom_client,review_db):
    from modules.organizations.branding import get_or_create_branding
    branding=get_or_create_branding(review_db)
    branding.footer_text='Line\n'*50
    review_db.commit()
    identifier=create_invoice_order(bom_client)
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==422
    assert 'footer' in response.json()['detail'].lower()


def test_unsupported_glyph_is_safe_without_customer_text_in_logs(bom_client, caplog):
    identifier=create_invoice_order(bom_client,customer_name='Synthetic private customer \U0001F9D1')
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==422
    assert 'Synthetic private customer' not in response.text
    assert 'Synthetic private customer' not in caplog.text


def test_uploaded_local_logo_is_embedded_without_external_fetch(bom_client,review_db,tmp_path,monkeypatch):
    from PIL import Image
    from modules.orders import invoice_generator
    from modules.organizations.branding import get_or_create_branding
    Image.new('RGB',(32,16),color=(22,180,90)).save(tmp_path/'logo.png')
    monkeypatch.setattr(invoice_generator,'_BRANDING_DIR',tmp_path)
    branding=get_or_create_branding(review_db)
    branding.logo_url='/static/branding/logo.png'
    review_db.commit()
    identifier=create_invoice_order(bom_client)
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==200
    assert len(PdfReader(BytesIO(response.content)).pages[0].images)==1
    branding.logo_url='https://external.example.invalid/logo.png'
    review_db.commit()
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==200
    assert len(PdfReader(BytesIO(response.content)).pages[0].images)==0


@pytest.mark.parametrize('operation',['add','update'])
def test_missing_price_is_unknown_in_every_item_response(bom_client,review_db,operation):
    review_db.execute(text('UPDATE products SET price=NULL WHERE id=101'))
    review_db.commit()
    identifier=create_invoice_order(bom_client)
    if operation=='add':
        response=bom_client.post(f'/orders/{identifier}/items',json={'product_id':101,'quantity':2})
    else:
        from core.dependencies import get_current_user
        bom_client.app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','group_id':1,'is_active':True}
        item=bom_client.get(f'/orders/{identifier}').json()['items'][0]['id']
        response=bom_client.patch(f'/orders/{identifier}/items/{item}',json={'unit_price':None})
    assert response.status_code==200
    assert response.json()['unit_price'] is None
    assert response.json()['subtotal'] is None


def test_real_branding_upload_reaches_customer_invoice(bom_client,tmp_path,monkeypatch):
    from PIL import Image
    from core.dependencies import get_current_user
    from modules.system import routes_settings
    from modules.orders import invoice_generator
    root=tmp_path/'modules'
    monkeypatch.setattr(routes_settings,'__file__',str(root/'system'/'routes_settings.py'))
    monkeypatch.setattr(invoice_generator,'_BRANDING_DIR',root.parent/'static'/'branding')
    bom_client.app.include_router(routes_settings.router)
    bom_client.app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','group_id':None,'is_active':True}
    image=BytesIO();Image.new('RGB',(32,16),color=(22,180,90)).save(image,format='PNG')
    uploaded=bom_client.post('/branding/logo',files={'file':('synthetic-logo.png',image.getvalue(),'image/png')})
    assert uploaded.status_code==200
    assert uploaded.json()['logo_url'].startswith('/static/branding/logo-') and uploaded.json()['logo_url'].endswith('.png')
    identifier=create_invoice_order(bom_client)
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==200
    assert len(PdfReader(BytesIO(response.content)).pages[0].images)==1


@pytest.mark.parametrize('boundary',['outside_symlink','oversized_file','oversized_pixels','traversal'])
def test_invoice_logo_boundaries_do_not_embed_untrusted_assets(bom_client,review_db,tmp_path,monkeypatch,boundary):
    from PIL import Image
    from modules.orders import invoice_generator
    from modules.organizations.branding import get_or_create_branding
    root=tmp_path/'branding';root.mkdir()
    monkeypatch.setattr(invoice_generator,'_BRANDING_DIR',root)
    branding=get_or_create_branding(review_db)
    branding.logo_url='/static/branding/logo.png'
    if boundary=='outside_symlink':
        outside=tmp_path/'outside.png';Image.new('RGB',(32,16)).save(outside)
        (root/'logo.png').symlink_to(outside)
    elif boundary=='oversized_file':
        (root/'logo.png').write_bytes(b'x'*(5*1024*1024+1))
    elif boundary=='oversized_pixels':
        Image.new('RGB',(2001,2000)).save(root/'logo.png')
    else:
        Image.new('RGB',(32,16)).save(tmp_path/'outside.png')
        branding.logo_url='/static/branding/../outside.png'
    review_db.commit()
    identifier=create_invoice_order(bom_client)
    response=bom_client.get(f'/orders/{identifier}/invoice.pdf')
    assert response.status_code==200
    assert len(PdfReader(BytesIO(response.content)).pages[0].images)==0
