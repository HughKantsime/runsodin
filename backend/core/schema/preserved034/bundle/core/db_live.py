"""Call-time resolution of core.db_utils.get_db.

core/db_utils.py stays byte-identical to its qualified source (migration 029
pins it), so the call-time wrapper lives here. Modules import this as `get_db`
so they never keep a get_db captured at import while core.db_utils.get_db was
temporarily replaced (a stale replacement would point at a foreign database
for the rest of the run).
"""


def live_get_db(*args, **kwargs):
    """core.db_utils.get_db, looked up when called."""
    from core import db_utils
    return db_utils.get_db(*args, **kwargs)
