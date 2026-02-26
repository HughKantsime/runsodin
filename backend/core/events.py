# core/events.py — canonical event type definitions
# All cross-module communication should use these constants as event_type values.

# Printer events
PRINTER_STATE_CHANGED = "printer.state_changed"       # {printer_id, old_state, new_state, telemetry}
PRINTER_CONNECTED = "printer.connected"               # {printer_id, api_type}
PRINTER_DISCONNECTED = "printer.disconnected"         # {printer_id, reason}
PRINTER_ERROR = "printer.error"                       # {printer_id, code, message, severity}
PRINTER_HMS_CODE = "printer.hms_code"                 # {printer_id, code, message, severity}

# Job events
JOB_CREATED = "job.created"                           # {job_id, model_id, printer_id}
JOB_STARTED = "job.started"                           # {job_id, printer_id}
JOB_COMPLETED = "job.completed"                       # {job_id, printer_id, duration, filament_used}
JOB_FAILED = "job.failed"                             # {job_id, printer_id, error}
JOB_CANCELLED = "job.cancelled"                       # {job_id, reason}

# Vision events
DETECTION_TRIGGERED = "vision.detection"              # {printer_id, detection_type, confidence, frame_url}
DETECTION_AUTO_PAUSE = "vision.auto_pause"            # {printer_id, detection_type}

# Inventory events
SPOOL_LOW = "inventory.spool_low"                     # {spool_id, remaining_grams, threshold}
SPOOL_EMPTY = "inventory.spool_empty"                 # {spool_id}
CONSUMABLE_LOW = "inventory.consumable_low"           # {consumable_id, current_stock, min_stock}

# System events
BACKUP_COMPLETED = "system.backup_completed"          # {filename, size}
LICENSE_CHANGED = "system.license_changed"            # {tier, features}
