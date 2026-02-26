# core/interfaces/org_settings.py
from abc import ABC, abstractmethod
from typing import Optional


class OrgSettingsProvider(ABC):
    """What modules need to read org-level config."""

    @abstractmethod
    def get_org_settings(self, org_id: int) -> dict: ...

    @abstractmethod
    def get_org_quiet_hours(self, org_id: int) -> dict: ...

    @abstractmethod
    def get_org_webhook(self, org_id: int) -> Optional[dict]: ...
