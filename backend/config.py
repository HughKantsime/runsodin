"""
O.D.I.N. — Configuration settings.

Re-export facade: canonical location is now backend/core/config.py.
All existing `from config import settings` imports continue to work.
"""

from core.config import Settings, settings  # noqa: F401
