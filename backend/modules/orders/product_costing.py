"""Complete product BOM estimates, without measured-cost attribution."""
import math


def _number(value, *, positive=False, integer=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (number <= 0 if positive else number < 0):
        return None
    if integer and not number.is_integer():
        return None
    return number


def estimated_product_cogs(product):
    printed = list(product.components)
    supplies = list(getattr(product, 'consumable_links', []))
    if not printed and not supplies:
        return None
    total = 0.0
    for links, reference, cost_field, quantity_field, whole in [
        (printed, 'model', 'cost_per_item', 'quantity_needed', True),
        (supplies, 'consumable', 'cost_per_unit', 'quantity_per_product', False),
    ]:
        for link in links:
            item = getattr(link, reference, None)
            if item is None:
                return None
            if reference == 'model' and item.org_id not in (None, product.org_id):
                return None
            cost = _number(getattr(item, cost_field, None))
            quantity = _number(getattr(link, quantity_field, None), positive=True, integer=whole)
            if cost is None or quantity is None:
                return None
            amount = cost * quantity
            total += amount
            if not math.isfinite(amount) or not math.isfinite(total):
                return None
    return round(total, 2)


def estimated_order_financials(order, product_for):
    """BOM-estimated COGS, profit and margin for an order (never measured actuals).

    ``product_for(product_id)`` returns the Product or None. COGS is None when the
    order has no items or any line's BOM cost is unknown, so a partial sum is never
    reported as the order total. Unrecorded fees count as zero.
    """
    unknown = {"estimated_cogs": None, "estimated_profit": None, "estimated_margin_percent": None}
    items = list(order.items or [])
    if not items:
        return unknown
    cogs = 0.0
    for item in items:
        quantity = _number(item.quantity, integer=True)
        product = product_for(item.product_id) if item.product_id is not None else None
        unit = estimated_product_cogs(product) if product is not None else None
        if quantity is None or unit is None:
            return unknown
        cogs += unit * quantity
    if not math.isfinite(cogs):
        return unknown
    result = dict(unknown, estimated_cogs=round(cogs, 2))
    fees = 0.0
    for value in (order.platform_fees, order.payment_fees, order.shipping_cost):
        if value is not None:
            fee = _number(value)
            if fee is None:
                return result
            fees += fee
    revenue = _number(order.revenue)
    if revenue is None:
        return result
    profit = revenue - cogs - fees
    result["estimated_profit"] = round(profit, 2)
    if revenue > 0:
        result["estimated_margin_percent"] = round(profit / revenue * 100, 1)
    return result
