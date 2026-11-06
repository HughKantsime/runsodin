"""Printer and user quota checks; license tier policy stays in license_manager."""
def check_printer_limit(current_count: int):
    """Check if adding another printer would exceed the license limit."""
    from fastapi import HTTPException
    from license_manager import get_license
    license_info = get_license()
    if license_info.max_printers is not None and current_count >= license_info.max_printers:
        raise HTTPException(
            status_code=403,
            detail=f"Printer limit reached ({license_info.max_printers}). Upgrade to Pro for unlimited printers."
        )


def check_user_limit(current_count: int):
    """Check if adding another user would exceed the license limit."""
    from fastapi import HTTPException
    from license_manager import get_license
    license_info = get_license()
    if license_info.max_users is not None and current_count >= license_info.max_users:
        raise HTTPException(
            status_code=403,
            detail=f"User limit reached ({license_info.max_users}). Upgrade to Pro for unlimited users."
        )


