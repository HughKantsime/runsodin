"""Canonical printer identities shared by import and compatibility checks."""
from __future__ import annotations
import re
from typing import Any

_MACHINE_ALIASES = {
    "x1c": "x1c",
    "x1carbon": "x1c",
    "bambulabx1c": "x1c",
    "bambulabx1carbon": "x1c",
    "c11": "x1c",
    "o1d": "x1c",
    "p1s": "p1s",
    "bambulabp1s": "p1s",
    "blp001": "p1s",
    "p1p": "p1p",
    "bambulabp1p": "p1p",
    "blp002": "p1p",
    "h2d": "h2d",
    "bambulabh2d": "h2d",
    "blp003": "h2d",
    "h2s": "h2s",
    "bambulabh2s": "h2s",
    "h2c": "h2c",
    "bambulabh2c": "h2c",
    "a1": "a1",
    "n2s": "a1",
    "bambulaba1": "a1",
    "bla001": "a1",
    "a1mini": "a1-mini",
    "n1": "a1-mini",
    "bambulaba1mini": "a1-mini",
    "bla003": "a1-mini",
    "k1": "k1",
    "crealityk1": "k1",
}

def canonical_machine(value: Any) -> str | None:
    normalized = re.sub(r"[^a-z0-9+]", "", str(value or "").strip().lower())
    return _MACHINE_ALIASES.get(normalized)
