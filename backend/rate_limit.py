"""
O.D.I.N. — Shared slowapi rate limiter instance.

Re-export facade: canonical location is now backend/core/rate_limit.py.
All existing `from rate_limit import limiter` imports continue to work.
"""

from core.rate_limit import limiter  # noqa: F401
from slowapi.util import get_remote_address  # noqa: F401
