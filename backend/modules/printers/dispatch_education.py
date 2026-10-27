"""Education authority and reservation boundaries for physical dispatch."""
import logging
import json
log = logging.getLogger("odin.dispatch")

def _reconcile_education_denial(
    provider,
    context: dict,
    job_id: int,
    expected_revision: int,
    reason: str,
) -> None:
    from core.db import engine
    from sqlalchemy.orm import Session

    if not provider or not context:
        return
    try:
        with Session(engine) as db:
            provider.reconcile_dispatch_denial(
                db,
                submission_id=int(context["id"]),
                job_id=job_id,
                expected_revision=expected_revision,
                reason=reason,
            )
    except Exception as exc:
        log.error("[dispatch] Education denial reconciliation failed: %s", exc)



def _authorize_education_hardware_action(job: dict, creds: dict) -> tuple[bool, str] | None:
    """Final policy and compatibility check for Education-owned jobs."""
    # Preserve the dispatch facade's existing reconciliation seam after extraction.
    from modules.printers.dispatch import _reconcile_education_denial
    if job.get("education_submission_id") is None:
        from core.db import engine
        from core.model_preview_access import is_education_owned_file
        from sqlalchemy import text
        identifiers = {
            job.get("resolved_print_file_id"),
            job.get("model_print_file_id"),
            job.get("print_file_id"),
        } - {None}
        with engine.connect() as conn:
            for identifier in identifiers:
                path = conn.execute(
                    text("SELECT stored_path FROM print_files WHERE id=:id"),
                    {"id": identifier},
                ).scalar_one_or_none()
                if is_education_owned_file(conn, identifier, path):
                    return False, "Education file requires its approval workflow; no hardware command was sent"
        if job.get("model_id") is not None:
            from core.db import engine
            from core.registry import registry
            from sqlalchemy.orm import Session
            provider = registry.get_provider("EducationPolicyProvider")
            if provider is None:
                return False, "Education dispatch policy is unavailable; no hardware command was sent"
            try:
                with Session(engine) as db:
                    provider.assert_generic_model_reference_allowed(db, int(job["model_id"]))
            except Exception:
                return False, "Education resource requires its submission and approval workflow; no hardware command was sent"
        return None

    from core.db import engine
    from core.registry import registry
    from modules.printers.education_compatibility import evaluate_education_compatibility
    from sqlalchemy.orm import Session

    provider = registry.get_provider("EducationPolicyProvider")
    if provider is None:
        return False, "Education dispatch policy is unavailable; no hardware command was sent"

    expected_revision = job.get("education_lifecycle_revision")
    if expected_revision is None:
        return False, "Education lifecycle revision is missing; no hardware command was sent"

    with Session(engine) as db:
        context = provider.authorize_dispatch(
            db,
            job_id=int(job["id"]),
            printer_id=int(job["printer_id"]),
            expected_revision=int(expected_revision),
        )

    if not context or not context.get("authorized"):
        if context:
            _reconcile_education_denial(
                provider,
                context,
                int(job["id"]),
                int(expected_revision),
                "current approval or entitlement is no longer valid",
            )
        return False, "Education approval or printer entitlement changed; job returned to submitted"

    try:
        file_facts = json.loads(job.get("compatibility_facts_json") or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        file_facts = {}
    result = evaluate_education_compatibility(file_facts, creds)
    if not result["compatible"]:
        codes = sorted({reason["code"] for reason in result["reasons"]})
        _reconcile_education_denial(
            provider,
            context,
            int(job["id"]),
            int(expected_revision),
            "compatibility drift: " + ",".join(codes),
        )
        return False, (
            "Education compatibility check failed ("
            + ", ".join(codes)
            + "); job returned to submitted"
        )
    return True, result["engine_version"]



def _reserve_education_hardware_action(job: dict, extension: str):
    """Commit the Education correlation claim before any adapter call."""
    if job.get("education_submission_id") is None:
        return None, None

    from core.db import engine
    from core.registry import registry
    from sqlalchemy.orm import Session

    provider = registry.get_provider("EducationPolicyProvider")
    if provider is None:
        return None, "Education dispatch policy is unavailable; no hardware command was sent"
    try:
        with Session(engine) as db:
            reservation = provider.reserve_dispatch(
                db,
                job_id=int(job["id"]),
                printer_id=int(job["printer_id"]),
                expected_revision=int(job["education_lifecycle_revision"]),
                extension=extension,
            )
            if reservation:
                db.commit()
    except Exception as exc:
        log.error("[dispatch] Education reservation failed: %s", exc)
        return None, "Education dispatch reservation failed; no hardware command was sent"
    if not reservation:
        return None, "Education authority changed or this printer already has an active print claim"
    return (provider, reservation), None



def _cancel_education_reservation(provider, reservation: dict) -> bool:
    from core.db import engine
    from sqlalchemy.orm import Session

    try:
        with Session(engine) as db:
            cancelled = provider.cancel_dispatch_reservation(
                db,
                claim_id=reservation["claim_id"],
                submission_id=int(reservation["submission_id"]),
                job_id=int(reservation["job_id"]),
                printer_id=int(reservation["printer_id"]),
                expected_revision=int(reservation["authority_revision"]),
            )
            if cancelled:
                db.commit()
            return bool(cancelled)
    except Exception as exc:
        log.error("[dispatch] Education reservation cancellation failed: %s", exc)
        return False



def _confirm_education_dispatch(provider, reservation: dict) -> dict:
    from core.db import engine
    from sqlalchemy.orm import Session

    try:
        with Session(engine) as db:
            result = provider.confirm_dispatch_started(
                db,
                claim_id=reservation["claim_id"],
                submission_id=int(reservation["submission_id"]),
                job_id=int(reservation["job_id"]),
                printer_id=int(reservation["printer_id"]),
                expected_revision=int(reservation["authority_revision"]),
            )
            if result.get("transitioned"):
                db.commit()
            return result
    except Exception as exc:
        log.error("[dispatch] Education physical start confirmation failed: %s", exc)
        return {"education_owned": True, "authorized": False, "transitioned": False}
