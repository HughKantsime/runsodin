"""Printer-owned configuration equality and resource locking for outside bindings."""
from typing import Any, Protocol, runtime_checkable

from fastapi import HTTPException
from core.registry import registry


class PrinterResourceRefused(RuntimeError):
    """A resource disappeared before its admission lock was acquired."""


@runtime_checkable
class PrinterBindingProvider(Protocol):
    def configuration_key(self, db: Any, printer_id: int) -> str | None: ...
    def recipe_configuration_key(self, db: Any, printer_id: int, *, since: Any = None) -> str | None: ...
    def lock_resource(self, view: Any, table: str, identifier: int, column: str) -> None: ...


def require_printer_binding_provider() -> PrinterBindingProvider:
    provider = registry.get_provider("PrinterBindingProvider")
    if not isinstance(provider, PrinterBindingProvider):
        raise HTTPException(503, "Printer binding service unavailable")
    return provider
