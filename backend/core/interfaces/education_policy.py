"""Cross-module contract for Education ownership and lifecycle policy."""

import re
from abc import ABC, abstractmethod
from pathlib import PurePath


_ODIN_TOKEN_RE = re.compile(
    r"^odin-([0-9a-f]{32})(?:\.(?:3mf|gcode|bgcode))?$", re.IGNORECASE
)


def parse_education_token(value: str | None) -> str | None:
    """Return the normalized ODIN correlation token from a reported filename."""
    if not value:
        return None
    basename = PurePath(str(value).replace("\\", "/")).name
    match = _ODIN_TOKEN_RE.fullmatch(basename)
    return match.group(1).lower() if match else None


def is_education_reserved_filename(value: str | None) -> bool:
    return parse_education_token(value) is not None


def generic_archive_audience_sql() -> str:
    """Absent/older authorities exclude every identifiable Education source."""
    return (
        "(a.education_submission_id IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM education_submissions raw_s WHERE raw_s.print_file_id=a.print_file_id OR raw_s.job_id=a.job_id)"
        " AND NOT EXISTS (SELECT 1 FROM education_upload_operations raw_o JOIN print_files raw_f"
        " ON raw_f.id=a.print_file_id WHERE raw_o.final_path=raw_f.stored_path AND raw_o.final_path IS NOT NULL))"
    )


def assert_generic_model_reference_allowed(db, model_id: int | None) -> None:
    """Delegate source admission only; caller owns audience and transaction."""
    if model_id is None:
        return
    from fastapi import HTTPException
    from core.registry import registry
    policy = registry.get_provider("EducationPolicyProvider")
    guard = getattr(policy, "assert_generic_model_reference_allowed", None)
    if not callable(guard):
        raise HTTPException(status_code=503, detail="Education policy unavailable")
    guard(db, model_id)


class EducationPolicyProvider(ABC):
    def archive_audience_sql(self) -> str:
        """Older authorities fail closed for Education-marked archive history.

        Return a correlated predicate using archive alias a and bound principal
        parameters _arc_org, _arc_user, _arc_admin. Never include user text.

        Security contract: callers splice the returned string into SQL text
        as trusted in-process plugin SQL (semgrep sites are annotated on that
        basis). The hook must return constant SQL: the same string on every
        call, built only from literals in the plugin's own code, with every
        value referenced as a bound parameter. It must never interpolate
        request, principal, database or configuration values.
        """
        return generic_archive_audience_sql()

    @abstractmethod
    def assert_file_preview_allowed(self, db, *, file_id: int, principal: dict) -> None:
        """Authorize Education file previews using current submission visibility."""

    @abstractmethod
    def assert_generic_model_reference_allowed(self, db, model_id: int) -> None:
        """Reject reuse of Education files/models outside its approval lifecycle."""

    @abstractmethod
    def assert_generic_job_mutation_allowed(self, db, job_id: int) -> None:
        """Reject generic mutations/clones of Education-owned jobs."""

    @abstractmethod
    def assert_user_tenant_change_allowed(self, db, user_id: int) -> None:
        """Reject tenant reassignment while Education owns user references."""

    @abstractmethod
    def assert_user_hard_delete_allowed(self, db, user_id: int) -> None:
        """Reject hard delete while Education owns user history."""

    @abstractmethod
    def assert_org_hard_delete_allowed(self, db, org_id: int) -> None:
        """Reject hard delete while Education owns tenant history."""

    @abstractmethod
    def assert_printer_tenant_change_or_delete_allowed(self, db, printer_id: int) -> None:
        """Reject printer reassignment/deletion while Education owns it."""

    @abstractmethod
    def printer_is_currently_entitled(
        self, db, *, org_id: int, cost_center_id: int, printer_id: int
    ) -> bool:
        """Resolve current center/printer entitlement for dispatch policy."""

    @abstractmethod
    def authorize_dispatch(
        self, db, *, job_id: int, printer_id: int, expected_revision: int
    ) -> dict | None:
        """Return current Education dispatch context, or None for a generic job."""

    @abstractmethod
    def reconcile_dispatch_denial(
        self,
        db,
        *,
        submission_id: int,
        job_id: int,
        expected_revision: int,
        reason: str,
    ) -> bool:
        """Atomically return a denied Education dispatch to submitted state."""

    @abstractmethod
    def reserve_dispatch(
        self, db, *, job_id: int, printer_id: int, expected_revision: int, extension: str
    ) -> dict | None:
        """Reserve an opaque correlation token before touching hardware."""

    def lock_dispatch_reservation(self, db, *, reservation: dict) -> bool:
        """Lock and validate the exact existing pre-command authority in caller TX.

        Older providers refuse admission rather than accepting stale authority.
        This method never commits or starts device I/O.
        """
        return False

    @abstractmethod
    def cancel_dispatch_reservation(self, db, **kwargs) -> bool:
        """Cancel only the caller's still-current reserved claim."""

    @abstractmethod
    def confirm_dispatch_started(self, db, **kwargs) -> dict:
        """Advance Education authority after hardware accepts a print."""

    def start_identity_matches(self, context, *identities):
        return False

    def discover_start_authority(self, db, **kwargs):
        """Optional preinsert candidate discovery; absence grants no authority."""
        return None

    def recovery_visibility(self, db, *, evidence, principal):
        """Existing submission audience for sanitized recovery inspection."""
        return False

    def recovery_terminal_resolved(self, db, *, evidence):
        """Prove authorized terminal lifecycle and exact receipt before release."""
        return False

    def recovery_authority(self, db, *, evidence, principal, lock=False):
        """Authorize retained recovery without scheduled-only admission rules."""
        return False

    def lock_start_authority(self, db, context):
        return False

    def recheck_start_authority(self, db, context):
        return False

    def finalize_start_authority(self, db, **kwargs):
        raise RuntimeError('Education prepared start unavailable')

    @abstractmethod
    def claim_monitor_observation(self, db, **kwargs) -> dict:
        """Claim an exact-token monitor observation or quarantine it."""

    @abstractmethod
    def classify_monitor_observation(self, db, **kwargs) -> dict:
        """Classify a monitor packet before generic side effects."""

    @abstractmethod
    def terminal_monitor_observation(self, db, **kwargs) -> dict:
        """Apply factual Education terminal state atomically."""

    @abstractmethod
    def resolve_active_monitor_observation(self, db, *, printer_id: int) -> dict:
        """Resolve the unique durable Education observation after restart."""

    @abstractmethod
    def scheduler_context(self, db, *, job_id: int) -> dict | None:
        """Return current Education scheduling context, or None for a generic job."""

    @abstractmethod
    def advance_schedule(
        self, db, *, submission_id: int, job_id: int, printer_id: int, expected_revision: int
    ) -> bool:
        """CAS an authorized pending Education submission to scheduled without committing."""

    @abstractmethod
    def reset_stale_schedule(
        self, db, *, submission_id: int, job_id: int, printer_id: int, expected_revision: int
    ) -> bool:
        """CAS a still-valid stale schedule back to pending without committing."""

    @abstractmethod
    def reconcile_schedule_denial(
        self,
        db,
        *,
        submission_id: int,
        job_id: int,
        expected_revision: int,
        reason: str,
    ) -> bool:
        """Return a drifted Education schedule to submitted without committing."""
