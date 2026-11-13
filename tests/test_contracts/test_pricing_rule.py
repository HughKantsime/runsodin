"""Pricing Calculator fixes: one rule, markup named correctly, optional selling fees and plate handling.

Spec: .claude/specs/pricing-calculator-fixes.md. The table tests/fixtures/pricing_rule_cases.json
is also read by frontend/src/utils/pricingRule.test.ts, so the page's live preview and these
endpoints give the same numbers.
"""
import json
from pathlib import Path

import pytest

from modules.models_library.pricing_rule import price_item
from modules.models_library.services import DEFAULT_PRICING_CONFIG
from tests.test_contracts.test_cert_E_ana_backend import ana  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_product_bom_integrity import bom_client  # noqa: F401

pytestmark = pytest.mark.usefixtures("pro_license")
CASES = json.loads((Path(__file__).resolve().parents[1] / "fixtures/pricing_rule_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_rule_matches_the_shared_table(case):
    got = price_item(case["config"], **case["input"])
    assert set(got) == set(case["expected"])
    for key, expected in case["expected"].items():
        assert got[key] == pytest.approx(expected, abs=1e-6), key


def _price_before_2_3_0(config, material_cost, print_hours, markup):
    """The formula as it stood in routes/pricing.py before this change, written out again."""
    labor = (config["post_processing_min"] + config["packing_min"] + config["support_min"]) / 60 * config["hourly_rate"]
    electricity = (config["printer_wattage"] / 1000) * print_hours * config["electricity_rate"]
    depreciation = (config["printer_cost"] / config["printer_lifespan"]) * print_hours
    base = material_cost + labor + electricity + depreciation + config["packaging_cost"] + config["other_costs"]
    subtotal = base + base * (config["failure_rate"] / 100) + (
        config["monthly_rent"] / config["parts_per_month"] if config["parts_per_month"] > 0 else 0)
    return subtotal, subtotal * (1 + markup / 100)


@pytest.mark.parametrize("material, hours, units, markup", [
    (1.25, 2, 1, 50.0), (0.0, 0.0, 1, 0.0), (7.3, 11.75, 6, 135.0), (0.01, 0.02, 24, 12.5)])
def test_a_configuration_saved_before_this_change_prices_exactly_as_before(material, hours, units, markup):
    config = dict(DEFAULT_PRICING_CONFIG, monthly_rent=640.0, parts_per_month=320)
    assert not {"selling_fee_percent", "selling_fee_fixed", "plate_handling_min"} & set(config)
    subtotal, price = _price_before_2_3_0(config, material, hours, markup)
    got = price_item(config, material_cost=material, print_hours=hours, units_per_plate=units, markup_percent=markup)
    assert got["subtotal"] == subtotal and got["price"] == price       # exact, not approximately
    assert got["selling_fees"] == 0 and got["price_before_fees"] == price


def test_markup_and_margin_of_price_are_two_views_of_one_number():
    got = price_item(dict(DEFAULT_PRICING_CONFIG), material_cost=1.0, print_hours=1.0, markup_percent=50)
    assert got["margin_of_price_percent"] == pytest.approx(100 / 3)
    assert price_item(dict(DEFAULT_PRICING_CONFIG), material_cost=1.0, print_hours=1.0, markup_percent=0)["margin_of_price_percent"] == 0


def test_the_fee_is_covered_and_the_markup_still_applies_to_cost():
    config = dict(DEFAULT_PRICING_CONFIG, selling_fee_percent=10, selling_fee_fixed=0.30)
    got = price_item(config, material_cost=2.0, print_hours=1.0, markup_percent=50)
    # What is left after the marketplace takes its share is exactly cost plus markup.
    kept = got["price"] - (got["price"] * 0.10 + 0.30)
    assert kept == pytest.approx(got["subtotal"] * 1.5)
    assert got["selling_fees"] == pytest.approx(got["price"] * 0.10 + 0.30)


def test_a_fee_of_100_percent_or_more_has_no_price():
    with pytest.raises(ValueError):
        price_item(dict(DEFAULT_PRICING_CONFIG, selling_fee_percent=100), material_cost=1, print_hours=1)


def test_plate_handling_is_shared_between_the_units_on_a_plate():
    config = dict(DEFAULT_PRICING_CONFIG, plate_handling_min=6, hourly_rate=60)
    one = price_item(config, material_cost=0, print_hours=0, units_per_plate=1)["labor"]
    six = price_item(config, material_cost=0, print_hours=0, units_per_plate=6)["labor"]
    assert one - six == pytest.approx(5.0)        # 6 min vs 1 min of handling at $1 a minute


# ---------------- HTTP ----------------

def test_new_settings_are_optional_validated_and_never_change_an_untouched_configuration(ana):
    client, identity, db = ana
    identity.update(role="admin")
    untouched = client.get("/pricing-config").json()
    assert not {"selling_fee_percent", "selling_fee_fixed", "plate_handling_min"} & set(untouched)
    assert client.put("/pricing-config", json={"selling_fee_percent": 100}).status_code == 422
    assert client.put("/pricing-config", json={"selling_fee_percent": -1}).status_code == 422
    assert client.put("/pricing-config", json={"selling_fee_fixed": -0.01}).status_code == 422
    assert client.put("/pricing-config", json={"plate_handling_min": None}).status_code == 422
    saved = client.put("/pricing-config", json={"selling_fee_percent": 2.9, "selling_fee_fixed": 0.3, "plate_handling_min": 4})
    assert saved.status_code == 200, saved.text
    body = client.get("/pricing-config").json()
    assert (body["selling_fee_percent"], body["selling_fee_fixed"], body["plate_handling_min"]) == (2.9, 0.3, 4)
    assert body["default_margin"] == untouched["default_margin"]      # the stored markup is not reinterpreted


def test_model_cost_names_the_markup_and_reports_fees_and_margin(ana):
    client, identity, db = ana
    identity.update(role="admin")
    model_id = client.get("/models-with-pricing").json()[0]["id"]
    before = client.get(f"/models/{model_id}/cost").json()
    assert before["markup_percent"] == before["margin_percent"]
    assert before["selling_fees"] == 0
    expected_margin = round((before["suggested_price"] - before["subtotal"]) / before["suggested_price"] * 100, 1) if before["suggested_price"] else 0
    assert before["margin_of_price_percent"] == pytest.approx(expected_margin, abs=0.2)
    assert client.put("/pricing-config", json={"selling_fee_percent": 10}).status_code == 200
    after = client.get(f"/models/{model_id}/cost").json()
    assert after["subtotal"] == before["subtotal"] and after["markup_percent"] == before["markup_percent"]
    assert after["suggested_price"] == pytest.approx(before["suggested_price"] / 0.9, abs=0.01)
    assert after["selling_fees"] == pytest.approx(after["suggested_price"] * 0.10, abs=0.01)


def test_the_models_list_reports_the_markup_used_and_the_margin(ana):
    client, identity, db = ana
    row = client.get("/models-with-pricing").json()[0]
    assert row["effective_markup_percent"] == row["margin_percent"]
    assert "margin_of_price_percent" in row
    # "markup_percent" stays the model's own override (None when the model has none).
    assert "markup_percent" in row
