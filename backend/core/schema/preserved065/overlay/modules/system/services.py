"""System module entry points for other modules."""
from .update_check import run_check


def check_for_scheduled_update(db, started):
    run_check(db, started=started)
