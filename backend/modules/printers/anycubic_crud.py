"""Anycubic LAN credential rules shared by printer create and update routes."""

from fastapi import HTTPException


def validate_credentials(api_type, api_key):
    """Reject credentials for the credential-free Anycubic LAN protocol."""
    if api_type == "anycubic_lan" and api_key:
        raise HTTPException(400, "Anycubic LAN does not accept credentials")


def prepare_update_credentials(update_data, current_api_type):
    """Clear stored credentials when updating an Anycubic LAN printer."""
    if update_data.get("api_type", current_api_type) == "anycubic_lan":
        validate_credentials("anycubic_lan", update_data.get("api_key"))
        update_data["api_key"] = None
