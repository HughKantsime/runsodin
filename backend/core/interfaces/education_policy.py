"""Cross-module contract for Education ownership and lifecycle policy."""

from abc import ABC, abstractmethod


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
