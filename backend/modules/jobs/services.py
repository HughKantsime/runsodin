"""The jobs module's cross-module service boundary.

Other modules (monitors, dispatch, education, notifications, printers) change a
job's status and react to printer deletion only through these names; the
implementations live in modules.jobs.state_machine and modules.jobs.pinning.
Education queue reads and ordering use this same boundary so scheduling and
dispatch share the jobs-owned rank and head-of-queue policy.
"""
from modules.jobs.education_order import education_order, waiting_rows, ordered_waiting, head_refusal
from modules.jobs.pinning import clear_pins_for_deleted_printer
from modules.jobs.state_machine import Expr, TransitionError, transition_job, transition_result

__all__ = ["education_order", "waiting_rows", "ordered_waiting", "head_refusal", "Expr", "TransitionError", "clear_pins_for_deleted_printer", "transition_job", "transition_result"]
