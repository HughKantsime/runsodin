"""Explicit prospective currency authority; caller owns transaction and audit."""
from sqlalchemy import text
from core.audit import log_audit

SUPPORTED_CURRENCIES = ('USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF', 'NZD')


def _scope(owner_scope, owner_key):
    if not ((owner_scope == 'global' and type(owner_key) is int and owner_key == 0)
            or (owner_scope == 'org' and type(owner_key) is int and owner_key > 0)):
        raise ValueError('Invalid finance owner scope')
    return {'scope': owner_scope, 'key': owner_key}


def read_currency(db, *, owner_scope, owner_key):
    parameters = _scope(owner_scope, owner_key)
    return db.execute(text('SELECT currency FROM finance_currency_settings '
                           'WHERE owner_scope=:scope AND owner_key=:key'), parameters).scalar_one_or_none()


def write_currency(db, *, owner_scope, owner_key, currency):
    parameters = _scope(owner_scope, owner_key)
    if currency is not None and (type(currency) is not str or currency not in SUPPORTED_CURRENCIES):
        raise ValueError('Unsupported finance currency')
    parameters['currency'] = currency
    db.execute(text('INSERT INTO finance_currency_settings(owner_scope,owner_key,currency) '
                    'VALUES(:scope,:key,:currency) ON CONFLICT(owner_scope,owner_key) '
                    'DO UPDATE SET currency=excluded.currency'), parameters)
    log_audit(db, 'finance.currency_updated', 'finance_currency_settings',
              details={'owner_scope': owner_scope, 'owner_key': owner_key, 'currency': currency})
    return currency
