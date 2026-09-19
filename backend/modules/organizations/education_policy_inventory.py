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
    _entry("organizations.routes_education", "router:/education/*", "center/grant/printer", "read/mutate", "current tenant principal", (), "test_education_admin_core.py", "guarded"),
    _entry("organizations.routes_education_submissions", "router:POST /education/submissions", "submission/job/model/file", "mutate", "active student grant", ("Education outbox",), "test_education_submission_upload.py", "guarded"),
    _entry("organizations.education_submission_service", "process_sliced_3mf_submission", "submission/job/model/file", "mutate", "active student grant", ("Education outbox",), "test_education_submission_upload.py", "guarded"),
    _entry("organizations.routes_education_reviews", "router:GET/POST /education/submissions/*", "submission/job/printer", "read/mutate", "submitter or active manager/tenant admin", ("Education outbox",), "test_education_review_workflow.py", "guarded"),
    _entry("organizations.education_review_service", "approve/reject/list", "submission/job/printer", "read/mutate", "current tenant audience", ("printers.services", "Education outbox"), "test_education_review_workflow.py", "guarded"),
    _entry("printers.services", "evaluate_submission_compatibility", "print file/printer/slots", "read", "trusted Education review service", (), "test_education_review_workflow.py", "guarded"),
    _entry("organizations.education_policy", "authorize_dispatch/reconcile_dispatch_denial", "submission/job/printer", "dispatch", "trusted dispatch worker", ("printers.dispatch",), "test_education_dispatch_policy.py", "guarded"),
    _entry("organizations.routes_users", "update/delete user and group", "user/group", "lifecycle", "tenant admin/superadmin", (), "test_education_policy_inventory.py", "guarded"),
    _entry("organizations.routes", "delete_org", "organization", "lifecycle", "superadmin", (), "test_education_policy_inventory.py", "guarded"),
    _entry("printers.routes_crud", "update/delete printer", "printer", "lifecycle", "tenant admin", (), "test_education_policy_inventory.py", "guarded"),
    _entry("core.app.websocket", "websocket_endpoint", "event", "notify", "explicit live user", ("browser websocket",), "test_education_policy_inventory.py", "guarded"),
    _entry("printers.dispatch", "dispatch_job", "submission/job/printer", "dispatch", "trusted dispatch worker", ("printer adapter",), "test_education_dispatch_policy.py", "guarded"),
    _entry("printers.smart_plug", "printer state subscriber", "submission/job/printer", "dispatch", "trusted physical-action worker", ("smart plug",), "test_education_dispatch_policy.py", "batch2_required"),
    _entry("printers.routes_smart_plug", "router:/printers/{id}/plug/*", "printer", "physical-control", "authorized operator/admin", ("smart plug",), "test_education_dispatch_policy.py", "batch2_required"),
    _entry("printers.routes_controls", "plate-clear/control routes", "submission/job/printer", "mutate", "authorized manager/admin", ("scheduler",), "test_education_dispatch_policy.py", "batch2_required"),
    _entry("printers.monitors.mqtt_job_lifecycle", "job lifecycle topic", "submission/job/printer", "telemetry-transition", "trusted monitor", ("event bus",), "test_education_monitor_policy.py", "batch2_required"),
    _entry("printers.monitors.mqtt_printer", "printer telemetry topic", "submission/job/printer", "telemetry-transition", "trusted monitor", ("event bus",), "test_education_monitor_policy.py", "batch2_required"),
    _entry("printers.monitors.moonraker_monitor", "Moonraker state poll", "submission/job/printer", "telemetry-transition", "trusted monitor", ("event bus",), "test_education_monitor_policy.py", "batch2_required"),
    _entry("printers.monitors.prusalink_monitor", "PrusaLink state poll", "submission/job/printer", "telemetry-transition", "trusted monitor", ("event bus",), "test_education_monitor_policy.py", "batch2_required"),
    _entry("printers.monitors.elegoo_monitor", "Elegoo state poll", "submission/job/printer", "telemetry-transition", "trusted monitor", ("event bus",), "test_education_monitor_policy.py", "batch2_required"),
    _entry("jobs.scheduler", "scheduler tick", "submission/job/printer", "schedule", "trusted scheduler", ("organizations.education_policy", "printers.services"), "test_education_scheduler_policy.py", "guarded"),
    _entry("notifications.job_events", "job.* topics", "submission/job/alert", "notify", "trusted notification worker", ("Education outbox",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.error_handling", "printer/job error topics", "submission/job/alert", "notify", "trusted notification worker", ("Education outbox",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.alert_dispatch", "dispatch_alert", "Education alert", "external-sink", "none for Education", ("email", "push", "webhook"), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.alert_dispatcher", "alert dispatcher worker", "Education alert", "external-sink", "none for Education", ("email", "push", "webhook"), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.quiet_hours", "digest collector", "Education alert", "external-sink", "none for Education", ("digest",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.mqtt_republish", "MQTT republisher", "Education event", "external-sink", "none for Education", ("MQTT",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("push.fanout", "push fanout", "Education alert", "external-sink", "none for Education", ("APNS", "web push"), "test_education_notification_privacy.py", "batch2_required"),
    _entry("vision.detection_thread", "vision alert emission", "submission/job/alert", "notify", "trusted vision worker", ("Education outbox",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("archives.archive", "archive creation/read", "model/file/archive", "retention", "resource policy audience", ("archive routes",), "test_education_resource_policy.py", "batch2_required"),
    _entry("archives.routes.tags", "tag routes", "model/file/archive", "read/mutate", "resource policy audience", (), "test_education_resource_policy.py", "batch2_required"),
    _entry("reporting.report_runner", "scheduled report delivery", "job/file/report", "external-sink", "none for Education", ("email",), "test_education_notification_privacy.py", "batch2_required"),
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
            "jobs.routes.jobs_agent",
            "jobs.routes.jobs_crud",
            "jobs.routes.jobs_lifecycle",
            "jobs.routes.presets",
            "jobs.scheduler_routes",
            "models_library.routes.models_crud",
            "models_library.routes.pricing",
            "models_library.routes.print_files",
            "models_library.services",
            "notifications.routes.alerts",
            "orders.routes.orders_crud",
            "orders.routes.products",
            "organizations.routes_sessions",
            "reporting.education_usage",
            "reporting.routes.analytics",
            "reporting.routes.exports",
            "reporting.routes.reports",
            "system.routes_admin",
            "system.routes_config",
            "system.routes_handoff",
            "system.routes_maintenance",
        )
    ),
    _entry("notifications.channels", "channel send methods", "Education alert", "external-sink", "none for Education", ("email", "webhook"), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.event_dispatcher", "event dispatch worker", "Education event", "external-sink", "none for Education", ("notification channels",), "test_education_notification_privacy.py", "batch2_required"),
    _entry("notifications.routes.webhooks", "webhook routes/worker", "Education event", "external-sink", "none for Education", ("webhook",), "test_education_notification_privacy.py", "batch2_required"),
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
    "modules/printers/routes_smart_plug.py",
    "modules/printers/monitors/prusalink_monitor.py",
    "modules/printers/monitors/elegoo_monitor.py",
    "modules/archives/routes/tags.py",
    "modules/notifications/channels.py",
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
