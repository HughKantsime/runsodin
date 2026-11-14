"""O.D.I.N. — Scheduler material routing.

Compares what a job needs with what a printer has loaded, using the AMS gate's own
material rule (core.print_payload.material_match), so the scheduler stops handing a
PETG job to a printer loaded with PLA. Spec: .claude/specs/filament-type-routing.md.

Mode (system_config key ``scheduler_material_mode``):
  require  a printer whose loaded materials are KNOWN and miss a needed material is
           not a candidate (default)
  prefer   nothing is excluded; a material match outranks any colour effect
  off      material is ignored (colour scoring only)

Unknown never excludes: a job with no recorded material, a printer with no recorded
slots, and any unrecognised label on either side all leave the printer a candidate,
so a missing estimate cannot stall the queue.
"""

from typing import Dict, Iterable, List, Optional, Set, Tuple

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from core import print_payload
from core.models import SystemConfig

MODE_KEY = "scheduler_material_mode"
REQUIRE, PREFER, OFF = "require", "prefer", "off"
MODES = (REQUIRE, PREFER, OFF)
DEFAULT_MODE = REQUIRE

# Added to a candidate's score in ``prefer``. Larger than any colour, setup, start
# time or load term, so a full material match outranks a colour-only match.
PREFER_BONUS = 10_000

Materials = Optional[Tuple[str, ...]]  # canonical labels; None = unknown

# One query for every pending job's file filaments and job material. The file is
# resolved exactly as core.print_file_binding.job_file_id does: the job's own file,
# else the model's file, else the model's newest file.
from core.dispatch_payload_selection import SELECTED_PRINT_FILE_ID_SQL

# The file a job prints is chosen by the one rule dispatch uses (Education submission, the job's
# own file, the model's file, else the model's newest file): never a second selection rule here.
# nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL plus the shared source-owned selection fragment; no values
_NEEDED_SQL = text(
    "SELECT j.id, j.filament_type, pf.filaments_json /* scheduler material plan */ "
    "FROM jobs j LEFT JOIN models m ON m.id = j.model_id "
    "LEFT JOIN education_submissions s ON s.job_id = j.id "
    "LEFT JOIN print_files pf ON pf.id = (" + SELECTED_PRINT_FILE_ID_SQL + ") "
    "WHERE j.status = 'pending' AND j.hold IS FALSE")
_LOADED_SQL = text("SELECT printer_id, filament_type FROM filament_slots")


def get_mode(db: Session) -> str:
    """The saved mode; ``require`` when nothing valid is saved."""
    row = db.query(SystemConfig).filter(SystemConfig.key == MODE_KEY).populate_existing().first()
    value = row.value if row is not None else None
    if isinstance(value, dict):
        value = value.get("mode")
    return value if value in MODES else DEFAULT_MODE


def set_mode(db: Session, mode: str) -> str:
    """Save the mode; the caller audits and commits. Returns the saved mode."""
    if mode not in MODES:
        raise ValueError(f"scheduler_material_mode must be one of {', '.join(MODES)}")
    row = db.query(SystemConfig).filter(SystemConfig.key == MODE_KEY).first()
    if row:
        row.value = {"mode": mode}
    else:
        db.add(SystemConfig(key=MODE_KEY, value={"mode": mode}))
    return mode


def _labels(values: Iterable) -> Tuple[str, ...]:
    seen: List[str] = []
    for value in values:
        label = print_payload.canonical_material(getattr(value, "value", value))
        if label is not None and label not in seen:
            seen.append(label)
    return tuple(seen)


def needed_materials(filaments_json, job_filament_type=None) -> Materials:
    """Distinct ``type`` values of the sliced file; the job's own filament type only when
    the file names none; None (unknown) otherwise.

    A file that names a material the gate's rule does not recognise ("PLA Basic") is unknown as
    a whole: the job's field must not stand in for it, or a known wrong answer would exclude a
    printer that an unknown never excludes.
    """
    try:
        types = [f.get("type") for f in print_payload.source_filaments(filaments_json)]
    except HTTPException:  # missing or invalid file metadata: no file information
        types = []
    named = [t for t in (getattr(t, "value", t) for t in types) if t is not None and str(t).strip()]
    if named:
        if any(print_payload.canonical_material(t) is None for t in named):
            return None
        return _labels(named) or None
    return _labels([job_filament_type]) or None


def loaded_materials(slot_types: Iterable) -> Materials:
    """Distinct non-empty loaded materials; None (unknown) when none is recorded, or
    when any recorded label is unrecognised (that spool could be anything)."""
    labels: List[str] = []
    for raw in slot_types:
        raw = getattr(raw, "value", raw)
        if raw is None or str(raw).strip().lower() in ("", "empty"):
            continue
        label = print_payload.canonical_material(raw)
        if label is None:
            return None
        if label not in labels:
            labels.append(label)
    return tuple(labels) or None


def missing_materials(loaded: Tuple[str, ...], needed: Tuple[str, ...]) -> Tuple[str, ...]:
    """The needed materials no loaded material matches (the gate's rule, not a copy)."""
    return tuple(n for n in needed
                 if not any(print_payload.material_match(n, l) is True for l in loaded))


def refusal_text(missing: Tuple[str, ...], pinned_name: Optional[str] = None, together: bool = False) -> str:
    """Why a job stays pending; shown in the run log and the run summary."""
    labels = " and ".join(missing)
    if pinned_name:
        return f"printer {pinned_name} does not have {labels} loaded"
    if together:
        return f"no printer has {labels} loaded together"
    return f"no printer has {labels} loaded"


class MaterialPlan:
    """Per-run material facts: built once, consulted per (job, printer)."""

    def __init__(self, mode: str = OFF,
                 needed: Optional[Dict[int, Materials]] = None,
                 loaded: Optional[Dict[int, Materials]] = None):
        self.mode = mode
        self.needed = needed or {}
        self.loaded = loaded or {}
        self._refused: Dict[int, List[Tuple[str, ...]]] = {}
        self._passed: Set[int] = set()

    @classmethod
    def load(cls, db: Session, printer_ids: Iterable[int]) -> "MaterialPlan":
        """Two queries for the whole run (none beyond the mode read when ``off``)."""
        plan = cls(get_mode(db))
        if plan.mode == OFF:
            return plan
        ids = set(printer_ids)
        by_printer: Dict[int, list] = {}
        for printer_id, filament_type in db.execute(_LOADED_SQL):
            if printer_id in ids:
                by_printer.setdefault(printer_id, []).append(filament_type)
        plan.loaded = {pid: loaded_materials(types) for pid, types in by_printer.items()}
        for job_id, filament_type, filaments_json in db.execute(_NEEDED_SQL):
            plan.needed[job_id] = needed_materials(filaments_json, filament_type)
        return plan

    def excludes(self, job_id: int, printer_id: int) -> bool:
        """True when ``require`` drops this printer for this job (known mismatch)."""
        if self.mode == OFF:
            return False
        needed, loaded = self.needed.get(job_id), self.loaded.get(printer_id)
        gap = missing_materials(loaded, needed) if needed and loaded else ()
        if gap and self.mode == REQUIRE:
            self._refused.setdefault(job_id, []).append(gap)
            return True
        self._passed.add(job_id)
        return False

    def bonus(self, job_id: int, printer_id: int) -> int:
        """``prefer`` only: score for the share of needed materials the printer has."""
        if self.mode != PREFER:
            return 0
        needed, loaded = self.needed.get(job_id), self.loaded.get(printer_id)
        if not needed or not loaded:
            return 0
        have = len(needed) - len(missing_materials(loaded, needed))
        return PREFER_BONUS * have // len(needed)

    def reason(self, job_id: int, pinned_name: Optional[str] = None) -> Optional[str]:
        """Set only when material is why the job has no printer: every printer that
        passed the other filters was refused for material."""
        if job_id in self._passed or job_id not in self._refused:
            return None
        gaps = self._refused[job_id]
        if pinned_name:
            return refusal_text(min(gaps, key=len), pinned_name)
        # Only what EVERY refused printer lacks may be called loaded nowhere. When each printer
        # lacks a different one (PLA here, PETG there), the true statement is about the set.
        everywhere = tuple(m for m in gaps[0] if all(m in gap for gap in gaps))
        if everywhere:
            return refusal_text(everywhere)
        return refusal_text(tuple(self.needed.get(job_id) or min(gaps, key=len)), together=True)
