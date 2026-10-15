"""Canonical printer identities shared by import and compatibility checks."""
from __future__ import annotations
import re
from typing import Any

_MACHINE_ALIASES = {
    "x1c": "x1c",
    "x1carbon": "x1c",
    "bambulabx1c": "x1c",
    "bambulabx1carbon": "x1c",
    "p1s": "p1s",
    "bambulabp1s": "p1s",
    "p1p": "p1p",
    "bambulabp1p": "p1p",
    "h2d": "h2d",
    "bambulabh2d": "h2d",
    "h2s": "h2s",
    "bambulabh2s": "h2s",
    "h2c": "h2c",
    "bambulabh2c": "h2c",
    "a1": "a1",
    "bambulaba1": "a1",
    "a1mini": "a1-mini",
    "bambulaba1mini": "a1-mini",
    "x1": "x1",
    "bambulabx1": "x1",
    "x1e": "x1e",
    "bambulabx1e": "x1e",
    "k1": "k1",
    "crealityk1": "k1",
}


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9+]", "", str(value or "").strip().lower())


def _bambu_model_id_aliases() -> dict[str, str]:
    # Bambu model ids come from the single Bambu model table so import,
    # monitor auto-detect and education compatibility cannot disagree.
    from modules.printers.printer_models import BAMBU_MODEL_IDS
    return {_normalize(code): _MACHINE_ALIASES[_normalize(name)] for code, name in BAMBU_MODEL_IDS.items()}


_MACHINE_ALIASES.update(_bambu_model_id_aliases())

def canonical_machine(value: Any) -> str | None:
    return _MACHINE_ALIASES.get(_normalize(value))
