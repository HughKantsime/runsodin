"""
organizations/services.py — OrgSettingsService

Implements the OrgSettingsProvider ABC so the organizations module can
expose its settings via the registry without forcing other modules to
import directly from organizations/routes.py.
"""

from typing import Optional

from core.interfaces.org_settings import OrgSettingsProvider
from modules.organizations.routes import _get_org_settings


class OrgSettingsService(OrgSettingsProvider):
    """Concrete OrgSettingsProvider backed by the groups.settings_json column."""

    def get_org_settings(self, db, org_id: int) -> dict:
        """Return the full merged org settings dict (all keys with defaults filled in)."""
        return _get_org_settings(db, org_id)

    def get_org_quiet_hours(self, db, org_id: int) -> dict:
        """Return quiet hours config for the org."""
        settings = _get_org_settings(db, org_id)
        return {
            "enabled": settings.get("quiet_hours_enabled", False),
            "start": settings.get("quiet_hours_start", "22:00"),
            "end": settings.get("quiet_hours_end", "07:00"),
        }

    def get_org_webhook(self, db, org_id: int) -> Optional[dict]:
        """Return webhook config dict if configured, else None."""
        settings = _get_org_settings(db, org_id)
        url = settings.get("webhook_url")
        if not url:
            return None
        return {
            "url": url,
            "type": settings.get("webhook_type", "generic"),
        }
