"""Local backend tests; no live-service fixtures or environment-file loading."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# The RBAC matrix imports its helpers as top-level modules, like tests/conftest.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))

# A file batch may contain only backend/tests, without the contract conftest.
os.environ.setdefault("JWT_SECRET_KEY", "test-key-for-contract-tests-only")
os.environ.setdefault("COOKIE_SECURE", "false")
