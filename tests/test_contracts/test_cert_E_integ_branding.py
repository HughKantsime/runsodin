"""Cert-E integ: F15 White-Label Branding — backend producer + invoice PDF.

Real settings router (/api/branding, /api/branding/logo) and real orders module
(GET /api/orders/{id}/invoice.pdf) on the bootstrapped schema. The branding JSON the
backend serves after a real admin save + logo upload is written as the producer wire
fixture consumed by frontend/src/CertEIntegBranding.cert.test.tsx.
"""
import json
import re
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import text

from tests.test_contracts.test_cert_D_orders_invoice_pdf import _contents, _fills, _order, _rgb, api_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

# Exercises Pro-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("pro_license")

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "frontend" / "src" / "test-fixtures" / "cert-E-integ-branding.json"
BRANDING_TSX = ROOT / "frontend" / "src" / "pages" / "admin" / "Branding.tsx"

SAVE = {
    "app_name": "Synthetic Northwind Farm",
    "app_subtitle": "Synthetic Production Floor",
    "primary_color": "#1f6feb",
    "accent_color": "#d63384",
    "font_display": "Space Grotesk, system-ui, sans-serif",
    "font_body": "Inter, system-ui, sans-serif",
    "font_mono": "JetBrains Mono, monospace",
    "footer_text": "Synthetic footer line",
}


@pytest.fixture
def brand_app(api_client, tmp_path, monkeypatch):  # noqa: F811
    from core.dependencies import get_current_user
    from modules.orders import invoice_generator
    from modules.system import routes_settings
    client, identity = api_client
    store = tmp_path / "branding"
    store.mkdir()
    monkeypatch.setenv("ODIN_BRANDING_DIRECTORY", str(store))
    monkeypatch.setattr(invoice_generator, "_BRANDING_DIR", store)
    client.app.include_router(routes_settings.router, prefix="/api")
    client.app.dependency_overrides[get_current_user] = lambda: {"id": 3, "role": "admin", "group_id": None,
                                                                 "is_active": True}
    return client, store


def _png(color):
    image = BytesIO()
    Image.new("RGB", (48, 24), color=color).save(image, format="PNG")
    return image.getvalue()


def _invoice(client):
    identifier = _order(client)
    response = client.get(f"/api/orders/{identifier}/invoice.pdf")
    assert response.status_code == 200 and response.content.startswith(b"%PDF-")
    return PdfReader(BytesIO(response.content))


def test_admin_branding_save_and_logo_upload_reach_ui_wire_and_invoice(brand_app):
    client, store = brand_app
    saved = client.put("/api/branding", json=SAVE)
    assert saved.status_code == 200, saved.text
    uploaded = client.post("/api/branding/logo", files={"file": ("synthetic-logo.png", _png((31, 111, 235)), "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    logo_url = uploaded.json()["logo_url"]
    assert re.fullmatch(r"/static/branding/logo-[0-9a-f]{32}\.png", logo_url)
    assert (store / logo_url.rsplit("/", 1)[1]).is_file()
    public = client.get("/api/branding").json()
    for key, value in SAVE.items():
        assert public[key] == value
    assert public["logo_url"] == logo_url

    document = _invoice(client)
    page = document.pages[0]
    content = page.extract_text()
    assert "Synthetic Northwind Farm" in content and "Synthetic footer line" in content
    assert len(page.images) == 1 and page.images[0].image.size == (48, 24)
    assert _fills(_contents(document)).count(_rgb("#1f6feb")) >= 2

    fixture = {k: public[k] for k in sorted(public)}
    fixture["logo_url"] = "/static/branding/logo-<immutable>.png"
    assert fixture.pop("updated_at")  # volatile timestamp
    if FIXTURE.exists():
        stored = json.loads(FIXTURE.read_text())
        assert stored == fixture, "Producer contract changed; reconcile the consumer explicitly"
    else:
        FIXTURE.write_text(json.dumps(fixture, indent=2) + "\n")


def test_svg_logo_is_accepted_for_ui_but_not_rendered_on_invoice(brand_app):
    """Branding reaches the invoice only for raster logos (invoice_generator.py:100 PNG/JPEG/WebP)."""
    client, store = brand_app
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20" fill="#1f6feb"/></svg>'
    uploaded = client.post("/api/branding/logo", files={"file": ("synthetic.svg", svg, "image/svg+xml")})
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["logo_url"].endswith(".svg")
    assert client.get("/api/branding").json()["logo_url"] == uploaded.json()["logo_url"]
    assert len(_invoice(client).pages[0].images) == 0


def _ui_font_lists():
    source = BRANDING_TSX.read_text()
    lists = {}
    for name in ("DISPLAY_FONTS", "BODY_FONTS", "MONO_FONTS"):
        block = source.split(f"const {name} = [", 1)[1].split("]\n", 1)[0]
        lists[name] = re.findall(r'\{ name: "([^"]+)", stack: "([^"]+)" \}', block)
    return lists


def test_every_ui_font_choice_is_accepted_and_persisted_by_backend(brand_app):
    client, _ = brand_app
    lists = _ui_font_lists()
    assert {k: len(v) for k, v in lists.items()} == {"DISPLAY_FONTS": 28, "BODY_FONTS": 24, "MONO_FONTS": 12}
    field = {"DISPLAY_FONTS": "font_display", "BODY_FONTS": "font_body", "MONO_FONTS": "font_mono"}
    for list_name, fonts in lists.items():
        for _, stack in fonts:
            response = client.put("/api/branding", json={field[list_name]: stack})
            assert response.status_code == 200, (stack, response.text)
            assert client.get("/api/branding").json()[field[list_name]] == stack


def test_font_inventory_is_64_entries_but_fewer_distinct_fonts():
    lists = _ui_font_lists()
    entries = sum(len(v) for v in lists.values())
    distinct = {name for fonts in lists.values() for name, _ in fonts}
    assert entries == 64
    assert len(distinct) == 46, sorted(distinct)  # incl. "System Default" and "System Mono"
