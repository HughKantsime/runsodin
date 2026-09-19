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


class EducationPolicyProvider(ABC):
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

    @abstractmethod
    def cancel_dispatch_reservation(self, db, **kwargs) -> bool:
        """Cancel only the caller's still-current reserved claim."""

    @abstractmethod
    def confirm_dispatch_started(self, db, **kwargs) -> dict:
        """Advance Education authority after hardware accepts a print."""

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
