"""cert-D orders: GET /api/orders/{id}/invoice.pdf produces a real fpdf2 PDF carrying branding.

Mounted through modules.orders.register(app, registry) so the advertised /api path is the
one exercised. The PDF bytes are parsed with pypdf (text, producer, colour operators, images).
"""
import re
from io import BytesIO

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


@pytest.fixture
def api_client(review_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.registry import registry
    import modules.orders as orders_module
    identity = {'id': 3, 'role': 'operator', 'group_id': 1, 'is_active': True}
    app = FastAPI()
    orders_module.register(app, registry)
    def request_db():
        with Session(review_db.get_bind()) as session:
            yield session
    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: identity
    review_db.execute(text("INSERT INTO products(id,name,sku,org_id,price) VALUES(101,'Dragon Figurine','DRG-1',1,12.5)"))
    review_db.commit()
    return TestClient(app), identity


def _rgb(hex_value):
    return tuple(int(hex_value[i:i + 2], 16) for i in (1, 3, 5))


def _fills(stream, op='rg'):
    """Every RGB colour set by a content-stream operator, as 0-255 integer triples."""
    return [tuple(round(float(v) * 255) for v in m) for m in re.findall(r'([\d.]+) ([\d.]+) ([\d.]+) ' + op + r'\b', stream)]


def _brand(review_db, **values):
    from modules.organizations.branding import get_or_create_branding
    branding = get_or_create_branding(review_db)
    for key, value in values.items():
        setattr(branding, key, value)
    review_db.commit()


def _order(client):
    response = client.post('/api/orders', json={'order_number': 'INV-2001', 'customer_name': 'Grace Buyer', 'revenue': 29,
                                                 'shipping_charged': 4, 'items': [{'product_id': 101, 'quantity': 2}]})
    assert response.status_code == 200, response.text
    return response.json()['id']


def _contents(document):
    return b'\n'.join(page.get_contents().get_data() for page in document.pages).decode('latin-1')


def test_api_invoice_pdf_is_fpdf2_generated_with_order_content_and_brand_name_and_color(api_client, review_db, monkeypatch):
    import fpdf
    from modules.orders import invoice_generator
    client, _ = api_client
    assert int(fpdf.FPDF_VERSION.split('.')[0]) >= 2 and invoice_generator.FPDF is fpdf.FPDF
    produced = []
    original = fpdf.FPDF.output
    def recording(self, *args, **kwargs):  # pass-through: the real fpdf2 output is returned unchanged
        value = original(self, *args, **kwargs)
        produced.append(bytes(value))
        return value
    monkeypatch.setattr(fpdf.FPDF, 'output', recording)
    _brand(review_db, app_name='Northwind Print Farm', primary_color='#123456')
    identifier = _order(client)
    response = client.get(f'/api/orders/{identifier}/invoice.pdf')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/pdf'
    assert response.headers['content-disposition'] == f'attachment; filename="invoice_{identifier}.pdf"'
    assert response.content.startswith(b'%PDF-') and response.content.rstrip().endswith(b'%%EOF')
    assert produced == [response.content]                         # served bytes are exactly fpdf2's output
    document = PdfReader(BytesIO(response.content))
    content = '\n'.join(page.extract_text() for page in document.pages)
    for expected in ('Northwind Print Farm', 'INV-2001', 'Grace Buyer', 'Dragon Figurine', 'DRG-1', '$12.50', '$25.00', '$4.00', '$29.00'):
        assert expected in content, expected
    stream = _contents(document)
    assert _fills(stream).count(_rgb('#123456')) >= 2             # brand primary: header band fill + app-name text
    # The /api/v1 alias serves the same generator.
    assert client.get(f'/api/v1/orders/{identifier}/invoice.pdf').content.startswith(b'%PDF-')


def test_changing_brand_color_and_name_changes_the_rendered_pdf(api_client, review_db):
    client, _ = api_client
    identifier = _order(client)
    _brand(review_db, app_name='First Brand', primary_color='#aa0000')
    first = PdfReader(BytesIO(client.get(f'/api/orders/{identifier}/invoice.pdf').content))
    _brand(review_db, app_name='Second Brand', primary_color='#00bb44')
    second = PdfReader(BytesIO(client.get(f'/api/orders/{identifier}/invoice.pdf').content))
    assert 'First Brand' in first.pages[0].extract_text() and 'First Brand' not in second.pages[0].extract_text()
    assert 'Second Brand' in second.pages[0].extract_text()
    assert _rgb('#aa0000') in _fills(_contents(first)) and _rgb('#aa0000') not in _fills(_contents(second))
    assert _rgb('#00bb44') in _fills(_contents(second)) and _rgb('#00bb44') not in _fills(_contents(first))


def test_uploaded_brand_logo_is_embedded_as_image_in_api_invoice(api_client, review_db, tmp_path, monkeypatch):
    from PIL import Image
    from modules.orders import invoice_generator
    client, _ = api_client
    Image.new('RGB', (40, 20), color=(18, 52, 86)).save(tmp_path / 'logo.png')
    monkeypatch.setattr(invoice_generator, '_BRANDING_DIR', tmp_path)
    identifier = _order(client)
    assert len(PdfReader(BytesIO(client.get(f'/api/orders/{identifier}/invoice.pdf').content)).pages[0].images) == 0
    _brand(review_db, logo_url='/static/branding/logo.png')
    images = PdfReader(BytesIO(client.get(f'/api/orders/{identifier}/invoice.pdf').content)).pages[0].images
    assert len(images) == 1 and images[0].image.size == (40, 20)


def test_viewer_cannot_download_invoice(api_client):
    client, identity = api_client
    identifier = _order(client)
    identity['role'] = 'viewer'
    assert client.get(f'/api/orders/{identifier}/invoice.pdf').status_code == 403
