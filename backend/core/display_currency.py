"""Display labels for amounts without a recorded transaction currency."""
from core.finance_currency import read_currency

CURRENCY_MINOR_UNITS = {'USD': 2, 'EUR': 2, 'GBP': 2, 'CAD': 2, 'AUD': 2, 'JPY': 0, 'CHF': 2, 'NZD': 2}


def display_currency(db, user):
    """The caller's current label for amounts without their own currency."""
    group = user.get("group_id")
    if type(group) is int and group > 0:
        currency = read_currency(db, owner_scope="org", owner_key=group)
        return currency, "organization" if currency else None
    if group is None and user.get("role") in ("viewer", "operator", "admin"):
        currency = read_currency(db, owner_scope="global", owner_key=0)
        return currency, "global" if currency else None
    return None, None
