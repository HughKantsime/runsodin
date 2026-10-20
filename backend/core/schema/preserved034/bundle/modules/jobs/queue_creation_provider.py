"""Stateless jobs-owned adapter; no authorization or transaction ownership."""
from modules.jobs.queue_creation import (
    _stage_prospective_queue_job, _stage_prospective_queue_values,
)


class QueueCreationService:
    def stage_job(self, db, job, actor):
        return _stage_prospective_queue_job(db, job, actor)

    def stage_values(self, db, values, actor):
        return _stage_prospective_queue_values(db, values, actor)
