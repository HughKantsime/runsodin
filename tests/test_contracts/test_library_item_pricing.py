"""Library cards and /cost share per-item estimates; the web consumes the same cases."""
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_E_ana_backend import ana  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_product_bom_integrity import bom_client  # noqa: F401

pytestmark = pytest.mark.usefixtures("pro_license")
CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures/library_item_pricing.json").read_text())


def _set_model(db, *, units=6, hours=12, markup=None):
    db.execute(text("UPDATE models SET color_requirements=:colors, units_per_bed=:units, "
                    "build_time_hours=:hours, markup_percent=:markup WHERE id=101"),
               dict(colors=json.dumps({"slot1": {"grams": 300}}), units=units, hours=hours, markup=markup))
    db.commit()
    db.expire_all()


def _estimates(client):
    response = client.get("/models-with-pricing")
    assert response.status_code == 200, response.text
    row = next(row for row in response.json() if row["id"] == 101)
    response = client.get("/models/101/cost")
    assert response.status_code == 200, response.text
    cost = response.json()
    assert row["estimated_cost"] == cost["subtotal"]
    assert row["suggested_price"] == cost["suggested_price"]
    assert row["effective_markup_percent"] == row["margin_percent"] == cost["markup_percent"]
    assert row["margin_of_price_percent"] == cost["margin_of_price_percent"]
    return row, cost


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_list_and_cost_match_the_card_and_calculator_cases(ana, case):
    client, identity, db = ana
    identity.update(role="admin")
    assert client.put("/pricing-config", json=case["config"]).status_code == 200
    saved = client.get("/pricing-config").json()
    model = case["model"]
    _set_model(db, units=model["units_per_bed"], hours=model["build_time_hours"], markup=model["markup_percent"])
    identity.update(role="viewer")
    row, cost = _estimates(client)
    for key, expected in case["expected"].items():
        assert row[key] == expected, key
    # Plate facts and the model's stored override are not replaced with derived item figures.
    for key in ("units_per_bed", "build_time_hours", "total_filament_grams", "markup_percent"):
        assert row[key] == model[key]
    assert cost["filament_grams"] == 300 / model["units_per_bed"]
    assert cost["print_hours"] == 12 / model["units_per_bed"]
    assert client.get("/pricing-config").json() == saved
    assert client.put("/pricing-config", json={"default_margin": 99}).status_code == 403


@pytest.mark.parametrize("units", [None, 0, -1, 1, 6])
@pytest.mark.parametrize("hours", [None, 0, 12])
def test_list_uses_the_cost_endpoints_unit_and_duration_fallbacks(ana, units, hours):
    client, _, db = ana
    _set_model(db, units=units, hours=hours)
    row, cost = _estimates(client)
    count = units if units and units > 0 else 1
    assert cost["print_hours"] == (1 if hours is None else hours) / count
    assert row["units_per_bed"] == units and row["build_time_hours"] == hours


@pytest.mark.parametrize("cost_per_gram", [0, 0.04])
def test_list_and_cost_keep_the_material_library_rate(ana, cost_per_gram):
    client, _, db = ana
    db.execute(text("INSERT INTO filament_library(id,brand,name,material,cost_per_gram) "
                    "VALUES(199,'Synthetic','Synthetic PLA','PLA',:rate)"), {"rate": cost_per_gram})
    _set_model(db)
    _, cost = _estimates(client)
    assert cost["costs"]["material"] == round(50 * cost_per_gram, 2)
    assert cost["cost_source"] == "per-material (PLA)"
