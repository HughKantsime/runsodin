"""Keep generic job mutations outside the Education approval lifecycle."""
from fastapi import HTTPException
from core.registry import registry


def assert_generic_job_mutation_allowed(db, job_id: int) -> None:
    policy = registry.get_provider("EducationPolicyProvider")
    if policy is None:
        raise HTTPException(status_code=503, detail="Education policy unavailable")
    policy.assert_generic_job_mutation_allowed(db, job_id)


def assert_generic_model_reference_allowed(db, model_id: int | None) -> None:
    if model_id is None:
        return
    policy = registry.get_provider("EducationPolicyProvider")
    if policy is None:
        raise HTTPException(status_code=503, detail="Education policy unavailable")
    policy.assert_generic_model_reference_allowed(db, model_id)
