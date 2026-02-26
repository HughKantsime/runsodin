"""
O.D.I.N. License Manager — core re-export.

The canonical implementation lives in backend/license_manager.py.
This module provides the core/ import path for future modules.

Phase 1 note: The implementation remains in license_manager.py (not moved here)
because test_license.py uses patch.object() to mock internal functions like
_find_license_file. Moving the implementation would break those patches.
The implementation will be migrated in a later phase once test mocking is updated.
"""

from license_manager import (  # noqa: F401
    ODIN_PUBLIC_KEY,
    TIERS,
    LICENSE_DIR,
    LICENSE_FILENAME,
    INSTALL_ID_FILENAME,
    CRYPTO_AVAILABLE,
    get_installation_id,
    LicenseInfo,
    _find_license_file,
    _verify_signature,
    load_license,
    save_license_file,
    get_license,
    require_feature,
    check_printer_limit,
    check_user_limit,
)
