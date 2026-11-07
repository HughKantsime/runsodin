"""Private reader boundary; exact caller authority and transaction remain owned."""
from typing import Any, Protocol, runtime_checkable
from fastapi import HTTPException
from core.registry import registry


@runtime_checkable
class AttemptValuationProvider(Protocol):
    def read_private_valuation(self, db: Any, request: Any, actor: dict, attempt_id: int) -> dict: ...
    def read_order_measured_costs(self, db: Any, request: Any, actor: dict, order_id: int) -> dict: ...


def require_attempt_valuation_provider() -> AttemptValuationProvider:
    provider=registry.get_provider('AttemptValuationProvider')
    if not isinstance(provider,AttemptValuationProvider) or not callable(getattr(provider,'read_private_valuation',None)):
        raise HTTPException(503,'Private valuation service unavailable',headers={'Cache-Control':'private, no-store'})
    return provider
