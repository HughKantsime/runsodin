# core/interfaces/event_bus.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Any


@dataclass
class Event:
    event_type: str     # e.g. "printer.state_changed", "job.completed"
    source_module: str  # e.g. "printers", "jobs"
    data: dict


class EventBus(ABC):
    """Central pub/sub for cross-module communication."""

    @abstractmethod
    def publish(self, event: Event) -> None: ...

    @abstractmethod
    def subscribe(self, event_type: str, handler: Callable[[Event], Any]) -> None: ...

    @abstractmethod
    def unsubscribe(self, event_type: str, handler: Callable) -> None: ...
