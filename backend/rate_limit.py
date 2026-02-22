"""
O.D.I.N. — Shared slowapi rate limiter instance.

Import this module in main.py and any router that needs @limiter.limit() decorators.
Key function: get_remote_address (IP-based limiting).
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
