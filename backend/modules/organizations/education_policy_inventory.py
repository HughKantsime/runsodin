"""Enforceable inventory of every Education-sensitive execution surface."""

from __future__ import annotations

import re
from pathlib import Path


INVENTORY_REQUIRED_FIELDS = frozenset(
    {
        "surface",
        "callable_or_topic",
        "resource_path",
        "operation",
        "actor_audience",
        "downstream_consumers",
        "focused_test",
        "disposition",
    }
)
ALLOWED_DISPOSITIONS = frozenset({"guarded", "batch2_required", "excluded_internal"})


def _entry(
    surface: str,
    callable_or_topic: str,
    resource_path: str,
    operation: str,
    actor_audience: str,
    downstream_consumers: tuple[str, ...],
    focused_test: str,
    disposition: str,
) -> dict:
    return {
        "surface": surface,
        "callable_or_topic": callable_or_topic,
        "resource_path": resource_path,
        "operation": operation,
        "actor_audience": actor_audience,
        "downstream_consumers": downstream_consumers,
        "focused_test": focused_test,
        "disposition": disposition,
    }


POLICY_INVENTORY = (
    _entry("jobs.queue_creation", "_stage_prospective_queue_job", "job/model/file/upload destination", "stage caller-authorized queue entry", "internal adapter; callers retain tenant and Education authorization; bound files require exact destination", ("queue",), "test_upload_binding_and_plates.py", "excluded_internal"),
    _entry("printers.routes_dispatch_recovery", "inspect_dispatch_recovery/hold_dispatch_recovery/retain_dispatch_recovery/release_dispatch_recovery", "printer/job/submission/monitor claim", "inspect/hold/retain/release", "owner and charged tenant admin; Education provider school/center authority; no Education revocation", ("recovery history",), "test_dispatch_recovery_actions.py", "guarded"),
    _entry("jobs.education_order", "education_order/ordered_waiting/head_refusal", "Education job/printer/dispatch claim", "order/head check", "trusted scheduler/dispatch; authoritative head check under target lock", ("scheduler", "automatic and explicit dispatch"), "test_education_followup_queues.py", "guarded"),
    _entry("organizations.education_queues", "list_queue/reorder_queue", "Education submission/job/printer/grant", "read/reorder", "live tenant admin or active manager; explicit-org platform reads only; target-serialized full-list revision check", ("Education workbench", "dispatch ordering"), "test_education_followup_queues.py", "guarded"),
    _entry("organizations.education_superadmin_restore", "restore_superadmin", "user/membership/grants/credentials", "restore platform admin", "verified interactive platform superadmin; typed target confirmation; no live Education work", (), "test_education_superadmin_restore.py", "guarded"),
    _entry("organizations.education_slice_info", "slice_info", "submission/file/printer/slots", "read verified metadata", "tenant reviewer or explicit-org read-only platform admin; entitled printer", (), "test_ctec_pilot.py", "guarded"),
    _entry("models_library.education_retention", "hide_model/assert_model_deletable", "model/submission", "hide/delete guard", "tenant-authorized model editor", (), "test_ctec_pilot.py", "guarded"),
    _entry("organizations.education_submission_archive", "archive_submission", "submission/job/claim", "soft archive", "tenant admin or active manager; terminal quiescent work", (), "test_ctec_pilot.py", "guarded"),
    _entry("models_library.upload_storage", "fresh actor/duplicate/model namespace helpers", "file/model/user/token", "authorize/serialize upload", "current operator with write:models; canonical Education file audience and exact owner namespace", ("generic print-file upload",), "test_print_file_upload_ownership_behavior.py", "guarded"),
    _entry("models_library.file_management", "list/read/delete/schedule generic file", "file/model/job/printer/submission", "read/delete/queue", "current scoped viewer/operator; canonical file audience; persisted Education lifecycle protection", ("generic queue",), "test_print_file_management_audience_behavior.py", "guarded"),
    _entry("archives.attempt_accounting", "capture_attempt_spools/account_archive", "physical attempt/job/file/spool", "capture/account", "trusted archive persistence; unresolved ownership leaves inventory unchanged", ("spool usage", "filament delivery outbox"), "test_archive_ownership_accounting_boundary.py", "guarded"),
    _entry("archives.ownership", "capture_content_owner", "job/file/model/Education submission", "read ownership", "trusted first-start or explicit superadmin repair", ("immutable archive ownership",), "test_archive_legacy_ownership_behavior.py", "guarded"),
    _entry("archives.legacy_ownership", "resolve_legacy_archive_ownership", "archive/job/file/Education source graph", "bounded ownership repair", "explicit superadmin with write:jobs; caller-owned atomic transaction", ("archive visibility",), "test_archive_legacy_ownership_behavior.py", "guarded"),
    _entry("organizations.routes_education", "router:/education/*", "center/grant/printer", "read/mutate", "current tenant principal", (), "test_education_admin_core.py", "guarded"),
    _entry("organizations.routes_education_submissions", "router:POST /education/submissions", "submission/job/model/file", "mutate", "active student grant", ("Education outbox",), "test_education_submission_upload.py", "guarded"),
    _entry("organizations.education_submission_service", "process_sliced_3mf_submission", "submission/job/model/file", "mutate", "active student grant", ("Education outbox",), "test_education_submission_upload.py", "guarded"),
    _entry("organizations.routes_education_reviews", "router:GET/POST /education/submissions/*", "submission/job/printer", "read/mutate", "submitter or active manager/tenant admin", ("Education outbox",), "test_education_review_workflow.py", "guarded"),
    _entry("organizations.education_review_service", "approve/reject/list", "submission/job/printer", "read/mutate", "current tenant audience", ("printers.services", "Education outbox"), "test_education_review_workflow.py", "guarded"),
    _entry("printers.services", "evaluate_submission_compatibility", "print file/printer/slots", "read", "trusted Education review service", (), "test_education_review_workflow.py", "guarded"),
    _entry("organizations.education_policy", "authorize_dispatch/reconcile_dispatch_denial/recovery_authority/recovery_terminal_resolved/recovery_visibility", "submission/job/printer", "dispatch", "trusted dispatch worker", ("printers.dispatch",), "test_education_dispatch_policy.py", "guarded"),
    _entry("organizations.routes_users", "update/delete user and group", "user/group", "lifecycle", "tenant admin/superadmin", (), "test_education_policy_inventory.py", "guarded"),
    _entry("organizations.routes", "delete_org", "organization", "lifecycle", "superadmin", (), "test_education_policy_inventory.py", "guarded"),
    _entry("printers.routes_crud", "update/delete printer", "printer", "lifecycle", "tenant admin", (), "test_education_policy_inventory.py", "guarded"),
    _entry("core.app.websocket", "websocket_endpoint", "event", "notify", "explicit live user", ("browser websocket",), "test_education_policy_inventory.py", "guarded"),
    _entry("printers.dispatch_admission", "admit", "job/printer/submission/retained claim", "dispatch-admission", "trusted dispatch worker; current scoped resources and exact committed claim", ("adapter-entry receipt",), "test_dispatch_receipt_admission_behavior.py", "guarded"),
    _entry("printers.dispatch", "dispatch_job", "submission/job/printer", "dispatch", "trusted dispatch worker", ("printer adapter",), "test_education_dispatch_policy.py", "guarded"),
    _entry("printers.smart_plug", "printer state subscriber", "submission/job/printer", "dispatch", "trusted physical-action worker", ("smart plug",), "test_education_dispatch_policy.py", "batch2_required"),
    _entry("printers.routes_smart_plug", "router:/printers/{id}/plug/*", "printer", "physical-control", "authorized operator/admin", ("smart plug",), "test_education_dispatch_policy.py", "batch2_required"),
    _entry("printers.routes_controls", "plate-clear/control routes", "submission/job/printer", "mutate", "authorized manager/admin", ("scheduler",), "test_education_dispatch_policy.py", "batch2_required"),
    _entry("printers.monitors.mqtt_job_lifecycle", "job lifecycle topic", "submission/job/printer", "telemetry-transition", "trusted monitor", ("Education outbox",), "test_education_monitor_policy.py", "guarded"),
    _entry("printers.monitors.mqtt_printer", "printer telemetry topic", "submission/job/printer", "telemetry-transition", "trusted monitor", ("Education outbox",), "test_education_monitor_policy.py", "guarded"),
    _entry("printers.monitors.moonraker_monitor", "Moonraker state poll", "submission/job/printer", "telemetry-transition", "trusted monitor", ("Education outbox",), "test_education_monitor_policy.py", "guarded"),
    _entry("printers.monitors.prusalink_monitor", "PrusaLink state poll", "submission/job/printer", "telemetry-transition", "trusted monitor", ("Education outbox",), "test_education_monitor_policy.py", "guarded"),
    _entry("printers.monitors.elegoo_monitor", "Elegoo state poll", "submission/job/printer", "telemetry-transition", "trusted monitor", ("Education outbox",), "test_education_monitor_policy.py", "guarded"),
    _entry("jobs.scheduler", "scheduler tick", "submission/job/printer", "schedule", "trusted scheduler", ("organizations.education_policy", "printers.services"), "test_education_scheduler_policy.py", "guarded"),
    _entry("notifications.job_events", "job.* topics", "submission/job/alert", "notify", "trusted notification worker", ("Education outbox",), "test_education_monitor_policy.py", "guarded"),
    _entry("jobs.state_machine", "transition_job (the only jobs.status writer)", "job", "transition", "callers' own Education guards (generic routes, Education policy predicates)", ("jobs.*", "organizations.education_policy", "printers.monitors", "notifications.job_events"), "test_cert_D_job_state_machine.py", "guarded"),
    _entry("jobs.pinning", "explicit printer pin parse/issue/delete-marking", "job/printer", "read/mark", "trusted callers (job routes, scheduler, printer delete)", ("jobs.scheduler", "printers.routes_crud"), "test_cert_D_jobsB_scheduler.py", "guarded"),
    _entry("notifications.error_handling", "printer/job error topics", "submission/job/alert", "notify", "trusted notification worker", ("Education outbox",), "test_education_monitor_policy.py", "guarded"),
    _entry("notifications.alert_dispatch", "dispatch_alert", "Education alert", "external-sink", "none for Education", ("email", "push", "webhook"), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.alert_dispatcher", "alert dispatcher worker", "Education alert", "external-sink", "none for Education", ("email", "push", "webhook"), "test_education_notification_privacy.py", "batch2_required"),
    # The outbox only replays job alerts through alert_dispatcher.stage_alert/_deliver_staged_alert
    # (same recipient policy and sinks as the dispatcher entry above).
    _entry("notifications.alert_outbox", "alert outbox dispatch and replay", "Education alert", "external-sink", "none for Education", ("email", "push", "webhook"), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.quiet_hours", "digest collector", "Education alert", "external-sink", "none for Education", ("digest",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.mqtt_republish", "MQTT republisher", "Education event", "external-sink", "none for Education", ("MQTT",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("push.fanout", "push fanout", "Education alert", "external-sink", "none for Education", ("APNS", "web push"), "test_education_notification_privacy.py", "batch2_required"),
    _entry("vision.detection_thread", "vision alert emission", "submission/job/alert", "notify", "trusted vision worker", ("Education outbox",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("archives.archive", "archive creation/read", "model/file/archive", "retention", "resource policy audience", ("archive routes",), "test_education_resource_policy.py", "batch2_required"),
    _entry("archives.routes.tags", "tag routes", "model/file/archive", "read/mutate", "resource policy audience", (), "test_education_resource_policy.py", "batch2_required"),
    _entry("reporting.report_runner", "scheduled report delivery", "job/file/report", "external-sink", "none for Education", ("email",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("orders.batch_production", "recipe qualification and inspected production receipt", "job/model/physical-attempt", "read/local-mutate", "exact tenant plus generic model admission and explicit Education submission exclusion", (), "test_batch_acceptance.py", "guarded"),
    _entry("orders.operations", "jobs/readiness", "job/model/file", "read readiness", "interactive tenant operator; exact charged tenant and explicit Education submission exclusion; generic file/model admission remains caller policy", ("operations preparation UI",), "test_operations_preparation.py", "batch2_required"),
    _entry("orders.production", "immutable order production reconciliation", "job/model/order-item", "mutate", "caller Education lifecycle guard plus immutable generic-production bindings", (), "test_order_fulfillment_behavior.py", "batch2_required"),
    *(
        _entry(
            surface,
            surface,
            "job/model/file/archive/report/alert",
            "read/mutate",
            "central Education resource-policy audience",
            (),
            "test_education_resource_policy.py",
            "batch2_required",
        )
        for surface in (
            "archives.routes.archives_crud",
            "archives.timelapse_capture",
            "jobs.bulk_actions",
            "jobs.routes.jobs_agent",
            "jobs.routes.jobs_crud",
            "jobs.routes.jobs_lifecycle",
            "jobs.routes.presets",
            "jobs.scheduler_routes",
            "models_library.routes._models_helpers",
            "models_library.routes.models_crud",
            "models_library.routes.pricing",
            "models_library.routes.print_files",
            "models_library.services",
            "notifications.routes.alerts",
            "orders.routes.orders_crud",
            "orders.routes.products",
            "organizations.routes_sessions",
            "reporting.analytics_window",
            "reporting.education_usage",
            "reporting.routes.analytics",
            "reporting.routes.exports",
            "reporting.routes.reports",
            "system.routes_admin",
            "system.routes_config",
            "system.routes_handoff",
        )
    ),
    _entry("system.routes_maintenance", "maintenance logs/status/create/delete", "printer/service history", "read/mutate", "own/unscoped/shared printer scope before previews and writes", (), "test_maintenance_behavior.py", "guarded"),
    _entry("notifications.channels", "channel send methods", "Education alert", "external-sink", "none for Education", ("email", "webhook"), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.event_dispatcher", "event dispatch worker", "Education event", "external-sink", "none for Education", ("notification channels",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.routes.webhooks", "webhook routes/worker", "Education event", "external-sink", "none for Education", ("webhook",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("archives.first_start_valuation", "freeze_unit_valuation", "attempt/job/file/material", "capture", "sealed admitted StartPlan and material fence", ("attempt accounting",), "test_first_start_callback_order_ownership.py", "guarded"),
    _entry("archives.timelapse_encode", "EncodeJournal", "job/printer/media lifetime", "encode/publication", "current capture graph and guarded recorded namespace", ("media worker", "media reads"), "test_timelapse_encoder_publication.py", "guarded"),
    _entry("archives.timelapse_frame_capture", "capture frame", "job/printer/file/media lifetime", "capture", "fenced capture owner and camera graph", ("FrameJournal",), "test_timelapse_capture_ownership.py", "guarded"),
    _entry("archives.timelapse_frame_namespace", "bind_or_open_namespace", "media lifetime/ownership anchor", "namespace", "caller transaction and exact media guard", ("frame capture", "encode"), "test_timelapse_frame_namespace.py", "guarded"),
    _entry("archives.valuation_content_visibility", "assert_content_visible", "model/file/path/submission", "private-read", "caller bound reader and fenced content closure", ("private valuation response",), "test_private_attempt_valuation_route.py", "guarded"),
    _entry("archives.valuation_financial_graph", "prove_financial_graph", "attempt/job/order/economic identity", "private-read", "caller reader and locked financial parents; supplies no reader authority", ("private valuation response",), "test_private_attempt_valuation_route.py", "guarded"),
    _entry("jobs.recent_terminal", "recent terminal projection", "job/file/submission/thumbnail", "read", "token scope and tenant/content policy audience", ("dashboard",), "test_recent_terminal_jobs.py", "guarded"),
    _entry("organizations.education_ams_payload_authority", "capture_payload_graph/lock_and_recheck_payload_graph", "submission/job/model/file/upload operation", "approve-payload", "current reviewer and exact committed upload graph", ("AMS approval",), "test_education_ams_payload_authority.py", "guarded"),
)


_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_SENSITIVE_SQL = re.compile(
    r"(?:FROM|JOIN|UPDATE|INTO|DELETE\s+FROM)\s+"
    r"(?:jobs|models|print_files|archives|alerts)\b|"
    r"db\.query\((?:Job|Model|PrintFile|Archive|Alert)\)",
    re.IGNORECASE,
)
_EXPLICIT_SURFACE_PATHS = (
    "core/app.py",
    "modules/organizations/routes_education.py",
    "modules/organizations/routes_education_submissions.py",
    "modules/organizations/routes_education_reviews.py",
    "modules/organizations/routes_users.py",
    "modules/organizations/routes.py",
    "modules/printers/routes_crud.py",
    "modules/printers/routes_dispatch_recovery.py",
    "modules/printers/routes_smart_plug.py",
    "modules/system/routes_maintenance.py",
    "modules/printers/monitors/prusalink_monitor.py",
    "modules/printers/monitors/elegoo_monitor.py",
    "modules/archives/routes/tags.py",
    "modules/notifications/channels.py",
    # Job status now flows through jobs.state_machine; these workers remain surfaces.
    "modules/notifications/job_events.py",
    "modules/notifications/error_handling.py",
    "modules/notifications/event_dispatcher.py",
    "modules/notifications/routes/webhooks.py",
    "modules/notifications/alert_dispatch.py",
    "modules/notifications/alert_dispatcher.py",
    "modules/notifications/quiet_hours.py",
    "modules/notifications/mqtt_republish.py",
    "modules/push/fanout.py",
    "modules/vision/detection_thread.py",
    "modules/reporting/report_runner.py",
)


def _surface_for_path(path: Path, backend_root: Path) -> str:
    parts = list(path.relative_to(backend_root).with_suffix("").parts)
    if parts and parts[0] == "modules":
        parts = parts[1:]
    surface = ".".join(parts)
    return "core.app.websocket" if surface == "core.app" else surface


def discover_execution_surfaces(backend_root: Path | None = None) -> set[str]:
    """Discover current sensitive SQL readers/mutators and registered sinks."""
    root = backend_root or _BACKEND_ROOT
    discovered: set[str] = set()
    modules_root = root / "modules"
    if modules_root.is_dir():
        for path in modules_root.rglob("*.py"):
            if path.resolve() == Path(__file__).resolve():
                continue
            source = path.read_text(encoding="utf-8")
            if _SENSITIVE_SQL.search(source) or "bus.subscribe(" in source:
                discovered.add(_surface_for_path(path, root))
    for relative in _EXPLICIT_SURFACE_PATHS:
        path = root / relative
        if path.is_file():
            discovered.add(_surface_for_path(path, root))
    return discovered


REGISTERED_EXECUTION_SURFACES = frozenset(discover_execution_surfaces())


def validate_policy_inventory(discovered_surfaces: set[str] | None = None) -> None:
    discovered = set(discovered_surfaces or REGISTERED_EXECUTION_SURFACES)
    inventoried: set[str] = set()
    for entry in POLICY_INVENTORY:
        if set(entry) != INVENTORY_REQUIRED_FIELDS:
            raise RuntimeError(f"Incomplete Education policy inventory entry: {entry!r}")
        if entry["surface"] in inventoried:
            raise RuntimeError(f"Duplicate Education policy surface: {entry['surface']}")
        if entry["disposition"] not in ALLOWED_DISPOSITIONS:
            raise RuntimeError(f"Invalid Education policy disposition: {entry!r}")
        if not entry["callable_or_topic"] or not entry["resource_path"]:
            raise RuntimeError(f"Unroutable Education policy entry: {entry!r}")
        if not entry["actor_audience"] or not entry["focused_test"]:
            raise RuntimeError(f"Untested Education policy entry: {entry!r}")
        inventoried.add(entry["surface"])
    if inventoried != discovered:
        missing = sorted(discovered - inventoried)
        stale = sorted(inventoried - discovered)
        raise RuntimeError(
            f"Education policy inventory drift: missing={missing!r} stale={stale!r}"
        )


validate_policy_inventory()
