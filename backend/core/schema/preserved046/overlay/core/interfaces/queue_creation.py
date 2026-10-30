"""Queue staging boundary; caller retains Session, actor and transaction."""
from typing import Any, Protocol, runtime_checkable
from fastapi import HTTPException
from core.registry import registry


@runtime_checkable
class QueueCreationProvider(Protocol):
    def stage_job(self, db: Any, job: Any, actor: dict) -> Any: ...
    def stage_values(self, db: Any, values: dict, actor: dict) -> Any: ...


def require_queue_creation_provider() -> QueueCreationProvider:
    provider = registry.get_provider('QueueCreationProvider')
    if not isinstance(provider, QueueCreationProvider) or any(
        not callable(getattr(provider, method, None)) for method in ('stage_job', 'stage_values')
    ):
        raise HTTPException(503, 'Queue creation service unavailable')
    return provider
