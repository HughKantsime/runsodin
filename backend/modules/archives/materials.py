"""Resolve materials for already-visible archive rows without changing accounting."""
import json
import math
from sqlalchemy import bindparam, text

from core.controlled_parts import file_visibility, job_visibility
from core.interfaces.education_policy import generic_archive_audience_sql
from core.fact_text import fact_text
from core.material_identity import canonical_reported_material
from core.rbac import check_org_access
from core.registry import registry


def _entries(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _group(pairs):
    totals = {}
    for raw, grams in pairs:
        material = canonical_reported_material(raw)
        if material is None and isinstance(raw, str):
            # A library can name a material outside the printer protocol's vocabulary.
            label = fact_text(raw.upper())
            if label and label not in {"UNKNOWN", "EMPTY", "OTHER", "NONE", "N/A"}:
                material = label
        if material:
            totals.setdefault(material, None)
            if type(grams) in (float, int) and math.isfinite(grams) and grams >= 0:
                totals[material] = (totals[material] or 0) + grams
    return [{"material": material, "grams": grams} for material, grams in totals.items()]


def _fields(db, rows, user):
    consumption = {r["id"]: _entries(r.get("consumption", r.get("consumption_json"))) for r in rows}
    ids = {e["spool_id"] for entries in consumption.values() for e in entries if type(e.get("spool_id")) is int}
    spools = {}
    if ids:
        query = text("SELECT s.id,s.org_id,f.material FROM spools s JOIN filament_library f ON f.id=s.filament_id "
                     "WHERE s.id IN :ids").bindparams(bindparam("ids", expanding=True))
        spools = {r.id: r for r in db.execute(query, {"ids": sorted(ids)}) if check_org_access(user, r.org_id)}

    # The archive audience includes Education's exact submission, model, file and upload binding.
    provider = registry.get_provider("EducationPolicyProvider")
    audience = getattr(provider, "archive_audience_sql", None)
    education = audience() if callable(audience) else generic_archive_audience_sql()
    files, params = file_visibility(user, "a.print_file_id")
    jobs, job_params = job_visibility(user, "j")
    query = text(f"""
        SELECT a.id,f.filaments_json,j.filament_type
        FROM print_archives a
        LEFT JOIN print_files f ON f.id=a.print_file_id AND (f.org_id IS NULL OR f.org_id=a.org_id)
          AND ({files}) AND NOT EXISTS (SELECT 1 FROM education_submissions other
              WHERE other.print_file_id=f.id AND (a.education_submission_id IS NULL OR other.id<>a.education_submission_id))
        LEFT JOIN jobs j ON j.id=a.job_id AND (j.charged_to_org_id IS NULL OR j.charged_to_org_id=a.org_id)
          AND ({jobs})
        WHERE a.id IN :ids AND ({education})
    """).bindparams(bindparam("ids", expanding=True))
    evidence = {r.id: r for r in db.execute(query, {**params, **job_params, "ids": list(consumption),
                "_arc_org": user.get("group_id"), "_arc_user": user.get("id"), "_arc_admin": int(user.get("role") == "admin")})}
    for row in rows:
        identifier = row["id"]
        spool_pairs = []
        for entry in consumption[identifier]:
            spool_id = entry.get("spool_id")
            spool = spools.get(spool_id) if type(spool_id) is int else None
            if spool and spool.org_id in (None, row.get("org_id")):
                spool_pairs.append((spool.material, entry.get("grams_used")))
        materials = _group(spool_pairs)
        source = "spool"
        fallback = evidence.get(identifier)
        if not materials and fallback:
            materials = _group((e.get("type"), e.get("used_grams")) for e in _entries(fallback.filaments_json))
            source = "file"
            if not materials:
                materials = _group([(fallback.filament_type, None)])
                source = "job"
        row.update(materials=materials, material=materials[0]["material"] if len(materials) == 1 else None,
                   material_source=source if materials else None)


def add_materials(db, rows, user):
    for start in range(0, len(rows), 200):
        _fields(db, rows[start:start + 200], user)
    return rows
