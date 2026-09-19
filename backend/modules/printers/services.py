"""Sanctioned cross-module printer services without credential exposure."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from modules.printers.education_compatibility import evaluate_education_compatibility


def evaluate_submission_compatibility(
    db: Session, *, org_id: int, print_file_id: int, printer_id: int
) -> dict | None:
    """Evaluate stored file evidence against current non-secret printer facts."""
    row = db.execute(
        text(
            "SELECT pf.compatibility_facts_json,p.api_type,p.bed_x_mm,p.bed_y_mm,"
            "p.nozzle_diameter,p.machine_type,p.model,p.name,p.is_active,p.shared "
            "FROM print_files pf JOIN printers p ON p.id=:printer_id "
            "WHERE pf.id=:file_id AND pf.org_id=:org_id AND p.org_id=:org_id"
        ),
        {"printer_id": printer_id, "file_id": print_file_id, "org_id": org_id},
    ).fetchone()
    if not row or not bool(row.is_active) or bool(row.shared):
        return None
    slots = db.execute(
        text(
            "SELECT filament_type FROM filament_slots WHERE printer_id=:printer_id "
            "AND filament_type IS NOT NULL AND filament_type NOT IN ('empty','Unknown','OTHER')"
        ),
        {"printer_id": printer_id},
    ).fetchall()
    try:
        file_facts = json.loads(row.compatibility_facts_json or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        file_facts = {}
    printer_facts = {
        "api_type": row.api_type,
        "bed_x_mm": row.bed_x_mm,
        "bed_y_mm": row.bed_y_mm,
        "nozzle_diameter": row.nozzle_diameter,
        "machine_type": row.machine_type,
        "model": row.model,
        "name": row.name,
        "active_materials": [slot.filament_type for slot in slots],
    }
    return evaluate_education_compatibility(file_facts, printer_facts)
