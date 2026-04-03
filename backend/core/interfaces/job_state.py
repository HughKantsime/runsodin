# core/interfaces/job_state.py
from abc import ABC, abstractmethod


class JobStateProvider(ABC):
    """What modules need from the jobs module."""

    @abstractmethod
    def get_pending_jobs(self, org_id: int = None) -> list: ...

    @abstractmethod
    def update_job_status(self, job_id: int, status: str) -> None: ...
