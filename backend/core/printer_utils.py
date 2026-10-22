"""
O.D.I.N. — Printer utility helpers.

Provides utilities used across the printers module and other modules
that interact with printer records.

Extracted from deps.py as part of the modular architecture refactor.
"""

from typing import Optional


def get_printer_api_key(printer) -> Optional[str]:
    """Get decrypted API key for a printer."""
    if getattr(printer, "api_type", None) == "anycubic_lan":
        return None
    if not printer.api_key:
        return None
    from core.crypto import decrypt
    return decrypt(printer.api_key)


# ---- Print state vocabulary (one place for every protocol) ----
# Bambu MQTT reports RUNNING / PAUSE; the Moonraker, PrusaLink and Elegoo
# monitors persist PRINTING / PAUSED. Code that decides "is this printer
# printing" must use these sets, never compare one protocol's string.
PRINTING_STATES = ("RUNNING", "PRINTING")
PAUSED_STATES = ("PAUSE", "PAUSED")
# Idle with the last print's plate still on the bed: Bambu IDLE/FINISH, Moonraker
# STANDBY/COMPLETE, PrusaLink and Elegoo IDLE/FINISHED.
IDLE_STATES = ("IDLE", "FINISH", "FINISHED", "STANDBY", "COMPLETE")


def state_sql_in(column: str, states, prefix: str) -> tuple[str, dict]:
    """``UPPER(column) IN (...)`` with bound parameters, for any state set above."""
    names = [f"{prefix}{i}" for i in range(len(states))]
    # TRIM+UPPER: the same normalisation as is_printing_state (strip + upper).
    return (f"UPPER(TRIM({column})) IN ({','.join(':' + n for n in names)})", dict(zip(names, states)))


def _normalized(state: Optional[str]) -> str:
    """strip(' ') + upper: exactly what SQL UPPER(TRIM(x)) does on every backend."""
    return (state or "").strip(" ").upper()


def is_printing_state(state: Optional[str]) -> bool:
    """True while a print is actively running (not paused), for any protocol."""
    return _normalized(state) in PRINTING_STATES


def is_idle_state(state: Optional[str]) -> bool:
    """True while the printer is idle or finished, for any protocol."""
    return _normalized(state) in IDLE_STATES
