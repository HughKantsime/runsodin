"""
Universal Printer Event Handler — re-export shim.

All public symbols are re-exported from their focused sub-modules.
Existing import paths (from modules.notifications.event_dispatcher import X)
continue to work unchanged.

Sub-modules:
  channels.py        — send_push_notification, send_webhook, send_email
  printer_health.py  — telemetry, online/offline, camera, care counters
  error_handling.py  — record_error, clear_error, HMS parsing
  job_events.py      — job_started, job_completed, progress, compat wrappers
  alert_dispatch.py  — dispatch_alert, check_low_spool, bed_cooled monitor
"""

# Notification channels
from modules.notifications.channels import (
    send_push_notification,
    send_webhook,
    send_email,
)

# Printer health / telemetry
from modules.notifications.printer_health import (
    update_telemetry,
    mark_online,
    mark_offline,
    discover_camera,
    increment_care_counters,
    increment_nozzle_lifecycle,
    reset_maintenance_counters,
)

# Error handling
from modules.notifications.error_handling import (
    record_error,
    clear_error,
    parse_hms_errors,
    process_hms_errors,
)

# Job events
from modules.notifications.job_events import (
    job_started,
    job_completed,
    update_job_progress,
    on_print_start,
    on_print_complete,
    on_print_failed,
    on_print_paused,
    on_progress_update,
)

# Alert dispatch
from modules.notifications.alert_dispatch import (
    dispatch_alert,
    check_low_spool,
)

__all__ = [
    "send_push_notification", "send_webhook", "send_email",
    "update_telemetry", "mark_online", "mark_offline", "discover_camera",
    "increment_care_counters", "increment_nozzle_lifecycle", "reset_maintenance_counters",
    "record_error", "clear_error", "parse_hms_errors", "process_hms_errors",
    "job_started", "job_completed", "update_job_progress",
    "on_print_start", "on_print_complete", "on_print_failed",
    "on_print_paused", "on_progress_update",
    "dispatch_alert", "check_low_spool",
]
