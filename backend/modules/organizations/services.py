"""
organizations/services.py — OrgSettingsService

Implements the OrgSettingsProvider ABC so the organizations module can
expose its settings via the registry without forcing other modules to
import directly from organizations/routes.py.
"""

from typing import Optional

from core.interfaces.org_settings import OrgSettingsProvider
from core.interfaces.education_policy import EducationPolicyProvider
from modules.organizations.education_policy import (
    advance_schedule,
    assert_generic_job_mutation_allowed,
    assert_generic_model_reference_allowed,
    authorize_dispatch,
    cancel_dispatch_reservation,
    claim_monitor_observation,
    discover_start_authority, lock_start_authority, recheck_start_authority, finalize_start_authority, start_identity_matches,
    classify_monitor_observation,
    confirm_dispatch_started,
    assert_org_hard_delete_allowed,
    assert_printer_tenant_change_or_delete_allowed,
    assert_user_hard_delete_allowed,
    assert_user_tenant_change_allowed,
    printer_is_currently_entitled,
    reserve_dispatch,
    reconcile_schedule_denial,
    reconcile_dispatch_denial,
    reset_stale_schedule,
    resolve_active_monitor_observation,
    scheduler_context,
    terminal_monitor_observation,
)
from modules.organizations.routes import _get_org_settings


class OrgSettingsService(OrgSettingsProvider):
    """Concrete OrgSettingsProvider backed by the groups.settings_json column."""

    def get_org_settings(self, db, org_id: int) -> dict:
        """Return the full merged org settings dict (all keys with defaults filled in)."""
        return _get_org_settings(db, org_id)

    def get_org_quiet_hours(self, db, org_id: int) -> dict:
        """Return quiet hours config for the org."""
        settings = _get_org_settings(db, org_id)
        return {
            "enabled": settings.get("quiet_hours_enabled", False),
            "start": settings.get("quiet_hours_start", "22:00"),
            "end": settings.get("quiet_hours_end", "07:00"),
        }

    def get_org_webhook(self, db, org_id: int) -> Optional[dict]:
        """Return webhook config dict if configured, else None."""
        settings = _get_org_settings(db, org_id)
        url = settings.get("webhook_url")
        if not url:
            return None
        return {
            "url": url,
            "type": settings.get("webhook_type", "generic"),
        }


class EducationPolicyService(EducationPolicyProvider):
    """Registry-facing adapter for the central Education policy."""

    def archive_audience_sql(self) -> str:
        from modules.organizations.education_review_service import archive_audience_sql
        return archive_audience_sql()

    def assert_file_preview_allowed(self, db, *, file_id: int, principal: dict) -> None:
        from modules.organizations.education_review_service import assert_file_preview_allowed
        assert_file_preview_allowed(db, file_id=file_id, principal=principal)

    def assert_generic_model_reference_allowed(self, db, model_id: int) -> None:
        assert_generic_model_reference_allowed(db, model_id)

    def assert_generic_job_mutation_allowed(self, db, job_id: int) -> None:
        assert_generic_job_mutation_allowed(db, job_id)

    def assert_user_tenant_change_allowed(self, db, user_id: int) -> None:
        assert_user_tenant_change_allowed(db, user_id)

    def assert_user_hard_delete_allowed(self, db, user_id: int) -> None:
        assert_user_hard_delete_allowed(db, user_id)

    def assert_org_hard_delete_allowed(self, db, org_id: int) -> None:
        assert_org_hard_delete_allowed(db, org_id)

    def assert_printer_tenant_change_or_delete_allowed(self, db, printer_id: int) -> None:
        assert_printer_tenant_change_or_delete_allowed(db, printer_id)

    def printer_is_currently_entitled(
        self, db, *, org_id: int, cost_center_id: int, printer_id: int
    ) -> bool:
        return printer_is_currently_entitled(
            db, org_id=org_id, cost_center_id=cost_center_id, printer_id=printer_id
        )

    def authorize_dispatch(
        self, db, *, job_id: int, printer_id: int, expected_revision: int
    ) -> dict | None:
        return authorize_dispatch(
            db,
            job_id=job_id,
            printer_id=printer_id,
            expected_revision=expected_revision,
        )

    def reconcile_dispatch_denial(
        self,
        db,
        *,
        submission_id: int,
        job_id: int,
        expected_revision: int,
        reason: str,
    ) -> bool:
        return reconcile_dispatch_denial(
            db,
            submission_id=submission_id,
            job_id=job_id,
            expected_revision=expected_revision,
            reason=reason,
        )

    def reserve_dispatch(
        self, db, *, job_id: int, printer_id: int, expected_revision: int, extension: str
    ) -> dict | None:
        return reserve_dispatch(
            db,
            job_id=job_id,
            printer_id=printer_id,
            expected_revision=expected_revision,
            extension=extension,
        )

    def lock_dispatch_reservation(self, db, *, reservation: dict) -> bool:
        from modules.organizations.education_policy import lock_dispatch_reservation
        return lock_dispatch_reservation(db, reservation=reservation)

    def cancel_dispatch_reservation(self, db, **kwargs) -> bool:
        return cancel_dispatch_reservation(db, **kwargs)

    def confirm_dispatch_started(self, db, **kwargs) -> dict:
        return confirm_dispatch_started(db, **kwargs)

    def start_identity_matches(self, context, *identities):
        return start_identity_matches(context, *identities)

    def discover_start_authority(self, db, **kwargs):
        return discover_start_authority(db, **kwargs)

    def recovery_visibility(self, db, *, evidence, principal):
        from modules.organizations.education_policy import recovery_visibility
        return recovery_visibility(db, evidence=evidence, principal=principal)

    def recovery_terminal_resolved(self, db, *, evidence):
        from modules.organizations.education_policy import recovery_terminal_resolved
        return recovery_terminal_resolved(db, evidence=evidence)

    def recovery_authority(self, db, *, evidence, principal, lock=False):
        from modules.organizations.education_policy import recovery_authority
        return recovery_authority(db, evidence=evidence, principal=principal, lock=lock)

    def lock_start_authority(self, db, context):
        return lock_start_authority(db, context)

    def recheck_start_authority(self, db, context):
        return recheck_start_authority(db, context)

    def finalize_start_authority(self, db, **kwargs):
        return finalize_start_authority(db, **kwargs)

    def claim_monitor_observation(self, db, **kwargs) -> dict:
        return claim_monitor_observation(db, **kwargs)

    def classify_monitor_observation(self, db, **kwargs) -> dict:
        return classify_monitor_observation(db, **kwargs)

    def terminal_monitor_observation(self, db, **kwargs) -> dict:
        return terminal_monitor_observation(db, **kwargs)

    def resolve_active_monitor_observation(self, db, *, printer_id: int) -> dict:
        return resolve_active_monitor_observation(db, printer_id=printer_id)

    def scheduler_context(self, db, *, job_id: int) -> dict | None:
        return scheduler_context(db, job_id=job_id)

    def advance_schedule(
        self, db, *, submission_id: int, job_id: int, printer_id: int, expected_revision: int
    ) -> bool:
        return advance_schedule(
            db,
            submission_id=submission_id,
            job_id=job_id,
            printer_id=printer_id,
            expected_revision=expected_revision,
        )

    def reset_stale_schedule(
        self, db, *, submission_id: int, job_id: int, printer_id: int, expected_revision: int
    ) -> bool:
        return reset_stale_schedule(
            db,
            submission_id=submission_id,
            job_id=job_id,
            printer_id=printer_id,
            expected_revision=expected_revision,
        )

    def reconcile_schedule_denial(
        self,
        db,
        *,
        submission_id: int,
        job_id: int,
        expected_revision: int,
        reason: str,
    ) -> bool:
        return reconcile_schedule_denial(
            db,
            submission_id=submission_id,
            job_id=job_id,
            expected_revision=expected_revision,
            reason=reason,
        )


def upload_storage_probe():
    """Shared read-only diagnostic view of the configured upload filesystem."""
    from modules.organizations.education_submission_service import UPLOAD_ROOT
    from modules.organizations.education_storage import storage_readiness
    return UPLOAD_ROOT, storage_readiness(UPLOAD_ROOT)
