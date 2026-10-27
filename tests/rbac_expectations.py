import os
"""Shared RBAC status expectations."""
def _pub():
    """Public endpoint — no auth needed at all."""
    return {"no_headers": 200, "api_key_only": None, "viewer": None, "operator": None, "admin": None}

def _setup_locked():
    """Setup endpoint — locked after completion."""
    return {"no_headers": 403, "api_key_only": None, "viewer": None, "operator": None, "admin": None}

def _api_read():
    """Standard authenticated GET — blocked without API key, allowed with key or JWT."""
    return {"no_headers": 401, "api_key_only": 200, "viewer": 200, "operator": 200, "admin": 200}

def _op_write():
    """Operator+ write — blocked for viewer."""
    return {"no_headers": 401, "api_key_only": 403, "viewer": 403, "operator": 200, "admin": 200}

def _admin_only():
    """Admin only — blocked for viewer and operator."""
    return {"no_headers": 401, "api_key_only": 403, "viewer": 403, "operator": 403, "admin": 200}

def _admin_read():
    """Admin-only read endpoint."""
    return {"no_headers": 401, "api_key_only": 403, "viewer": 403, "operator": 403, "admin": 200}


_ONLINE_LICENSE = {**_admin_only(), "admin": 400 if os.environ.get("RBAC_LICENSE_TIER", "community") == "community" else 502}

