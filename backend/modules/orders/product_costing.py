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
