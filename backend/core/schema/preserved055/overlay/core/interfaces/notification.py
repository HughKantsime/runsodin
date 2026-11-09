# core/interfaces/notification.py
from abc import ABC, abstractmethod
from typing import Optional


class NotificationDispatcher(ABC):
    """What modules call to send user-facing notifications."""

    @abstractmethod
    def dispatch(self, alert_type: str, severity: str, title: str,
                 message: str, printer_id: Optional[int] = None,
                 job_id: Optional[int] = None, org_id: Optional[int] = None) -> None:
        ...

    @abstractmethod
    def should_suppress(self, org_id: Optional[int] = None) -> bool:
        ...
