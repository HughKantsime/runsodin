"""One pricing rule for the Pricing Calculator and the model cost endpoints.

Spec: .claude/specs/pricing-calculator-fixes.md. The frontend preview
(frontend/src/utils/pricingRule.ts) is a line-for-line copy; both are checked against the same
table, tests/fixtures/pricing_rule_cases.json, so they cannot drift apart.

What the saved "default_margin" has always been is a MARKUP: price = cost x (1 + markup). The
stored key keeps its name; nothing saved is reinterpreted. The three newer settings
(selling_fee_percent, selling_fee_fixed, plate_handling_min) are optional and count as 0 when a
saved configuration does not have them, so an existing configuration prices exactly as before.
"""
import math


def _number(config, key, default=0.0):
    value = config.get(key)
    return default if value is None else value


def price_item(config, *, material_cost, print_hours, units_per_plate=1, markup_percent=None):
    """Cost and price of one sellable item.

    material_cost and print_hours are the caller's figures for the item (the caller divides a
    plate's grams and hours by its unit count where it prices per item). units_per_plate is used
    here only to share the per-plate handling minutes between the items on a plate.
    Raises ValueError when the selling fee percentage is 100 or more (no price can cover it).
    """
    units = units_per_plate if isinstance(units_per_plate, (int, float)) and units_per_plate >= 1 else 1
    item_minutes = _number(config, "post_processing_min") + _number(config, "packing_min") + _number(config, "support_min")
    labor = (item_minutes + _number(config, "plate_handling_min") / units) / 60 * _number(config, "hourly_rate")
    electricity = (_number(config, "printer_wattage") / 1000) * print_hours * _number(config, "electricity_rate")
    depreciation = (_number(config, "printer_cost") / config["printer_lifespan"]) * print_hours
    packaging = _number(config, "packaging_cost")
    other = _number(config, "other_costs")
    base = material_cost + labor + electricity + depreciation + packaging + other
    failure = base * (_number(config, "failure_rate") / 100)
    parts = _number(config, "parts_per_month")
    overhead = _number(config, "monthly_rent") / parts if parts > 0 else 0
    subtotal = base + failure + overhead

    markup = _number(config, "default_margin") if markup_percent is None else markup_percent
    before_fees = subtotal * (1 + markup / 100)
    fee_percent = _number(config, "selling_fee_percent")
    fee_fixed = _number(config, "selling_fee_fixed")
    if fee_percent >= 100:
        raise ValueError("selling_fee_percent must be under 100")
    # The fee is a share of the selling price, so the price is solved for: after the fee is
    # taken, what is left still covers cost plus markup. With no fees this is before_fees exactly.
    price = (before_fees + fee_fixed) / (1 - fee_percent / 100) if (fee_percent or fee_fixed) else before_fees
    fees = price - before_fees
    margin_of_price = (price - fees - subtotal) / price * 100 if price > 0 else 0.0
    return {
        "material": material_cost, "labor": labor, "electricity": electricity, "depreciation": depreciation,
        "packaging": packaging, "other": other, "failure": failure, "overhead": overhead,
        "subtotal": subtotal, "markup_percent": markup, "price_before_fees": before_fees,
        "selling_fees": fees, "price": price, "margin_of_price_percent": margin_of_price,
    }


def all_finite(result):
    return all(isinstance(value, (int, float)) and math.isfinite(value) for value in result.values())
