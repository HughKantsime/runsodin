"""The jobs module's cross-module service boundary.

Other modules (monitors, dispatch, education, notifications, printers) change a
job's status and react to printer deletion only through these names; the
implementations live in modules.jobs.state_machine and modules.jobs.pinning.
"""
from modules.jobs.pinning import clear_pins_for_deleted_printer
from modules.jobs.state_machine import Expr, TransitionError, transition_job, transition_result

__all__ = ["Expr", "TransitionError", "clear_pins_for_deleted_printer", "transition_job", "transition_result"]
