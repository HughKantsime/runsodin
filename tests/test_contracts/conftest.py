"""Shared conftest for contract tests.

Contract tests run without a live backend, but importing backend modules
(e.g. `from core.dependencies import ...`) triggers module-level code that
requires JWT_SECRET_KEY. Provide a safe test value so contract tests
don't depend on shell env.
"""

import os
import sys
from pathlib import Path

# Ensure backend/ is on sys.path for `from core.x import y` style imports.
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Provide a test JWT secret if one isn't set. Contract tests never mint
# real tokens with this — it's only consumed at import time. Using the
# same value across tests keeps cached module state consistent.
os.environ.setdefault("JWT_SECRET_KEY", "test-key-for-contract-tests-only")
