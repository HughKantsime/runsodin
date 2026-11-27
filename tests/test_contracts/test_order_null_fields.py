"""Order and inventory lists stay readable after edits and on older rows."""
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_consumable_behavior import consumable_client
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client


def test_order_status_and_item_quantity(bom_client, review_db):
    from core.dependencies import get_current_user
    bom_client.app.dependency_overrides[get_current_user] = lambda: {"id": 3, "role": "admin", "group_id": 1}
    created = bom_client.post("/orders", json={"items": [{"product_id": 101, "quantity": 3}]})
    assert created.status_code == 200
    order = created.json()
    order_path = f"/orders/{order['id']}"
    item = order["items"][0]
    assert bom_client.patch(order_path, json={"status": None}).status_code == 422
    assert bom_client.patch(f"{order_path}/items/{item['id']}", json={"quantity": None}).status_code == 422
    detail = bom_client.get(order_path).json()
    assert detail["status"] == "pending" and detail["items"][0]["quantity"] == 3

    review_db.execute(text("UPDATE orders SET status=NULL WHERE id=:id"), {"id": order["id"]})
    review_db.execute(text("UPDATE order_items SET quantity=NULL WHERE id=:id"), {"id": item["id"]})
    review_db.commit()
    summary, = bom_client.get("/orders").json()
    assert summary["status"] == "pending" and summary["fulfilled"] is False
    assert bom_client.get("/orders?status_filter=pending").json() == [summary]
    detail = bom_client.get(order_path).json()
    assert detail["total_items"] == detail["items"][0]["quantity"] == 1
    assert detail["items"][0]["subtotal"] == item["unit_price"]
    assert detail["items"][0]["can_confirm_production"] is False
    assert bom_client.patch(order_path, json={"notes": "Checked"}).status_code == 200
    item_path = f"{order_path}/items/{item['id']}"
    assert bom_client.patch(item_path, json={"quantity": 2}).status_code == 200
    assert bom_client.get(order_path).json()["items"][0]["can_confirm_production"] is True


@pytest.mark.parametrize("field", ["name", "unit", "current_stock", "min_stock", "status"])
def test_consumable_required_fields(consumable_client, field):
    client, _ = consumable_client
    created = client.post("/consumables", json={"name": "Carton", "current_stock": 3, "min_stock": 2})
    assert created.status_code == 200
    item = created.json()
    response = client.patch(f"/consumables/{item['id']}", json={field: None})
    assert response.status_code == 422
    listed, = client.get("/consumables").json()
    assert listed[field] == item[field]


def test_existing_consumable_nulls_use_defaults_in_stock_lists(consumable_client, review_db):
    client, _ = consumable_client
    created = client.post("/consumables", json={"name": "Carton"}).json()
    path = f"/consumables/{created['id']}"
    review_db.execute(text("UPDATE consumables SET current_stock=NULL, min_stock=NULL, status=NULL, unit=NULL WHERE id=:id"),
                      {"id": created["id"]})
    review_db.commit()
    detail = client.get(path).json()
    assert (detail["current_stock"], detail["min_stock"], detail["status"], detail["unit"]) == (0, 0, "active", "piece")
    assert detail["is_low_stock"] is False
    assert [row["id"] for row in client.get("/consumables?status=active").json()] == [created["id"]]
    assert client.patch(path, json={"min_stock": 2}).json()["is_low_stock"] is True
    assert [row["id"] for row in client.get("/consumables/low-stock").json()] == [created["id"]]


def test_changelog_covers_null_order_and_consumable_edits():
    changelog = (Path(__file__).resolve().parents[2] / "CHANGELOG.md").read_text()
    terms = ("order line", "consumable", "null", "status", "quantity", "unit", "stock")
    assert any(all(term in line.lower() for term in terms) for line in changelog.splitlines() if line.startswith("- "))
