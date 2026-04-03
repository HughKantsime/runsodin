"""
O.D.I.N. Order Fulfillment — Ceiling Division Tests

Tests the schedule_order() BOM explosion and job generation logic.
Runs as integration tests against a live server.

Run:
    pytest tests/test_order_math.py -v --tb=short
"""

import os
import pytest
import requests
import uuid
from helpers import login as _shared_login, auth_headers as _make_headers

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
ADMIN_USERNAME = os.environ["ADMIN_USERNAME"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _login(username, password):
    return _shared_login(BASE_URL, username, password)


def _headers(token):
    return _make_headers(token)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def admin_token():
    token = _login(ADMIN_USERNAME, ADMIN_PASSWORD)
    if not token:
        pytest.skip("Cannot login — server not running or credentials wrong")
    return token


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return _headers(admin_token)


@pytest.fixture()
def test_model(admin_headers):
    """Create a test model and clean up after."""
    tag = uuid.uuid4().hex[:6]
    r = requests.post(f"{BASE_URL}/api/models", json={
        "name": f"OrderMath_{tag}",
        "build_time_hours": 1.0,
    }, headers=admin_headers)
    assert r.status_code in (200, 201), f"Failed to create model: {r.text}"
    model = r.json()
    yield model
    requests.delete(f"{BASE_URL}/api/models/{model['id']}", headers=admin_headers)


@pytest.fixture()
def test_product(admin_headers):
    """Create a test product and clean up after."""
    tag = uuid.uuid4().hex[:6]
    r = requests.post(f"{BASE_URL}/api/products", json={
        "name": f"OrderMath_Product_{tag}",
        "price": 25.00,
    }, headers=admin_headers)
    assert r.status_code in (200, 201), f"Failed to create product: {r.text}"
    product = r.json()
    yield product
    requests.delete(f"{BASE_URL}/api/products/{product['id']}", headers=admin_headers)


def _create_order_with_item(admin_headers, product_id, quantity, unit_price=10.0):
    """Create an order with a single item. Returns order dict."""
    r = requests.post(f"{BASE_URL}/api/orders", json={
        "order_number": f"TEST-{uuid.uuid4().hex[:6]}",
        "customer_name": "Test Customer",
        "revenue": quantity * unit_price,
        "items": [{
            "product_id": product_id,
            "quantity": quantity,
            "unit_price": unit_price,
        }],
    }, headers=admin_headers)
    assert r.status_code in (200, 201), f"Failed to create order: {r.text}"
    return r.json()


def _add_component(admin_headers, product_id, model_id, quantity_needed=1):
    """Add a BOM component to a product."""
    r = requests.post(
        f"{BASE_URL}/api/products/{product_id}/components",
        json={"model_id": model_id, "quantity_needed": quantity_needed},
        headers=admin_headers,
    )
    assert r.status_code in (200, 201), f"Failed to add component: {r.text}"
    return r.json()


def _schedule_order(admin_headers, order_id):
    """POST /api/orders/{id}/schedule."""
    r = requests.post(
        f"{BASE_URL}/api/orders/{order_id}/schedule",
        headers=admin_headers,
    )
    return r


def _cleanup_order(admin_headers, order_id):
    """Delete an order (and cascade items/jobs)."""
    requests.delete(f"{BASE_URL}/api/orders/{order_id}", headers=admin_headers)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOrderNotFound:
    def test_schedule_missing_order(self, admin_headers):
        r = _schedule_order(admin_headers, 999999)
        assert r.status_code == 404


class TestEmptyOrder:
    def test_no_items(self, admin_headers):
        r = requests.post(f"{BASE_URL}/api/orders", json={
            "order_number": f"EMPTY-{uuid.uuid4().hex[:6]}",
        }, headers=admin_headers)
        assert r.status_code in (200, 201)
        order = r.json()
        try:
            r2 = _schedule_order(admin_headers, order["id"])
            assert r2.status_code == 200
            data = r2.json()
            assert data["jobs_created"] == 0
        finally:
            _cleanup_order(admin_headers, order["id"])


class TestCeilingDivision:
    """
    Core math: pieces_needed = item.quantity * comp.quantity_needed
    jobs_needed = -(-pieces_needed // pieces_per_job)  # ceiling division
    qty_this_job = min(pieces_per_job, pieces_needed - (i * pieces_per_job))
    """

    def test_exact_fit_1x1(self, admin_headers, test_model, test_product):
        """1 item needing 1 piece, model fits 1 per bed → 1 job."""
        _add_component(admin_headers, test_product["id"], test_model["id"], quantity_needed=1)
        order = _create_order_with_item(admin_headers, test_product["id"], quantity=1)
        try:
            r = _schedule_order(admin_headers, order["id"])
            assert r.status_code == 200
            data = r.json()
            assert data["jobs_created"] == 1
            assert data["details"][0]["quantity_on_bed"] == 1
        finally:
            _cleanup_order(admin_headers, order["id"])

    def test_7_pieces_3_per_bed(self, admin_headers, test_product, admin_token):
        """
        7 pieces needed, 3 per bed → 3 jobs (3, 3, 1).
        Ceiling division: -(-7 // 3) = 3.
        """
        tag = uuid.uuid4().hex[:6]
        # Create model with quantity_per_bed = 3
        r = requests.post(f"{BASE_URL}/api/models", json={
            "name": f"OrderMath3Bed_{tag}",
            "build_time_hours": 0.5,
            "quantity_per_bed": 3,
        }, headers=_headers(admin_token))
        assert r.status_code in (200, 201)
        model = r.json()
        try:
            _add_component(admin_headers, test_product["id"], model["id"], quantity_needed=1)
            order = _create_order_with_item(admin_headers, test_product["id"], quantity=7)
            try:
                r2 = _schedule_order(admin_headers, order["id"])
                assert r2.status_code == 200
                data = r2.json()
                assert data["jobs_created"] == 3
                qtys = sorted([d["quantity_on_bed"] for d in data["details"]], reverse=True)
                assert qtys == [3, 3, 1]
            finally:
                _cleanup_order(admin_headers, order["id"])
        finally:
            requests.delete(f"{BASE_URL}/api/models/{model['id']}", headers=_headers(admin_token))

    def test_exact_multiple_10_by_5(self, admin_headers, test_product, admin_token):
        """
        10 pieces, 5 per bed → 2 jobs (5, 5). Exact division.
        """
        tag = uuid.uuid4().hex[:6]
        r = requests.post(f"{BASE_URL}/api/models", json={
            "name": f"OrderMath5Bed_{tag}",
            "build_time_hours": 0.5,
            "quantity_per_bed": 5,
        }, headers=_headers(admin_token))
        assert r.status_code in (200, 201)
        model = r.json()
        try:
            _add_component(admin_headers, test_product["id"], model["id"], quantity_needed=1)
            order = _create_order_with_item(admin_headers, test_product["id"], quantity=10)
            try:
                r2 = _schedule_order(admin_headers, order["id"])
                assert r2.status_code == 200
                data = r2.json()
                assert data["jobs_created"] == 2
                qtys = sorted([d["quantity_on_bed"] for d in data["details"]], reverse=True)
                assert qtys == [5, 5]
            finally:
                _cleanup_order(admin_headers, order["id"])
        finally:
            requests.delete(f"{BASE_URL}/api/models/{model['id']}", headers=_headers(admin_token))

    def test_one_piece_large_bed(self, admin_headers, test_product, admin_token):
        """
        1 piece needed, bed holds 3 → 1 job with quantity_on_bed=1.
        """
        tag = uuid.uuid4().hex[:6]
        r = requests.post(f"{BASE_URL}/api/models", json={
            "name": f"OrderMathLBed_{tag}",
            "build_time_hours": 0.5,
            "quantity_per_bed": 3,
        }, headers=_headers(admin_token))
        assert r.status_code in (200, 201)
        model = r.json()
        try:
            _add_component(admin_headers, test_product["id"], model["id"], quantity_needed=1)
            order = _create_order_with_item(admin_headers, test_product["id"], quantity=1)
            try:
                r2 = _schedule_order(admin_headers, order["id"])
                assert r2.status_code == 200
                data = r2.json()
                assert data["jobs_created"] == 1
                assert data["details"][0]["quantity_on_bed"] == 1
            finally:
                _cleanup_order(admin_headers, order["id"])
        finally:
            requests.delete(f"{BASE_URL}/api/models/{model['id']}", headers=_headers(admin_token))

    def test_order_status_changes_to_in_progress(self, admin_headers, test_model, test_product):
        """After scheduling, order status should become IN_PROGRESS."""
        _add_component(admin_headers, test_product["id"], test_model["id"], quantity_needed=1)
        order = _create_order_with_item(admin_headers, test_product["id"], quantity=1)
        try:
            _schedule_order(admin_headers, order["id"])
            r = requests.get(f"{BASE_URL}/api/orders/{order['id']}", headers=admin_headers)
            assert r.status_code == 200
            assert r.json()["status"] in ("in_progress", "IN_PROGRESS")
        finally:
            _cleanup_order(admin_headers, order["id"])


class TestBOMQuantityNeeded:
    """Test quantity_needed > 1 (e.g., 2 arms per figure)."""

    def test_multiplier(self, admin_headers, test_product, admin_token):
        """
        Product needs 2 pieces of a model per unit.
        Order qty=3 → pieces_needed = 3 * 2 = 6.
        Model fits 4 per bed → ceil(6/4) = 2 jobs (4, 2).
        """
        tag = uuid.uuid4().hex[:6]
        r = requests.post(f"{BASE_URL}/api/models", json={
            "name": f"OrderMathBOM_{tag}",
            "build_time_hours": 0.5,
            "quantity_per_bed": 4,
        }, headers=_headers(admin_token))
        assert r.status_code in (200, 201)
        model = r.json()
        try:
            _add_component(admin_headers, test_product["id"], model["id"], quantity_needed=2)
            order = _create_order_with_item(admin_headers, test_product["id"], quantity=3)
            try:
                r2 = _schedule_order(admin_headers, order["id"])
                assert r2.status_code == 200
                data = r2.json()
                assert data["jobs_created"] == 2
                qtys = sorted([d["quantity_on_bed"] for d in data["details"]], reverse=True)
                assert qtys == [4, 2]
            finally:
                _cleanup_order(admin_headers, order["id"])
        finally:
            requests.delete(f"{BASE_URL}/api/models/{model['id']}", headers=_headers(admin_token))
