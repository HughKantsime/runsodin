"""Source quantity attribution for operator job completion."""
import json
from sqlalchemy import text
from modules.models_library.models import Model


def completion_slot_grams(db, job):
    """Keep legacy model fallback and mapped file source IDs unchanged."""
    slot_grams = {}  # {slot_number: grams_to_deduct}

    # Source 1: Model color_requirements (has per-slot gram amounts)
    if job.model_id:
        model = db.query(Model).filter(Model.id == job.model_id).first()
        if model and model.color_requirements:
            req = model.color_requirements if isinstance(model.color_requirements, dict) else json.loads(model.color_requirements)
            for i, slot_key in enumerate(sorted(req.keys())):
                slot_data = req[slot_key]
                if isinstance(slot_data, dict) and slot_data.get("grams"):
                    slot_grams[i + 1] = float(slot_data["grams"])

    try:
        mapping = json.loads(job.ams_mapping) if isinstance(job.ams_mapping, str) else job.ams_mapping
    except (ValueError, TypeError):
        mapping = None
    mapped_sources = {i + 1 for i, target in enumerate(mapping) if target != -1} if isinstance(mapping, list) else set()
    if len(mapped_sources) > 1:
        slot_grams = {}
    if job.ams_mapping is None:
        # Source 2: Linked print file filaments (fallback if model has no gram data)
        if not slot_grams:
            # Check if a print_file is linked to this job
            pf_row = db.execute(text(
                "SELECT filaments_json FROM print_files WHERE job_id = :jid LIMIT 1"
            ), {"jid": job.id}).first()
            if pf_row and pf_row[0]:
                try:
                    pf_filaments = json.loads(pf_row[0]) if isinstance(pf_row[0], str) else pf_row[0]
                    for i, fil in enumerate(pf_filaments):
                        grams = fil.get("used_grams") or fil.get("weight_grams")
                        if grams:
                            slot_grams[i + 1] = float(grams)
                except (json.JSONDecodeError, TypeError):
                    pass

    else:
        # Mapped completion uses the selected file's source IDs, never model
        # color positions or a total attributed to just one of several trays.
        from core.print_file_binding import job_file_id
        from core.filament_sources import indexed_materials
        file_id = job_file_id(db, job.model_id, job.print_file_id)
        pf_row = db.execute(text(
            "SELECT filaments_json FROM print_files WHERE id = :id" if file_id else
            "SELECT filaments_json FROM print_files WHERE job_id = :id LIMIT 1"
        ), {"id": file_id or job.id}).first()
        if pf_row and pf_row[0]:
            try:
                import math
                pf_filaments = json.loads(pf_row[0]) if isinstance(pf_row[0], str) else pf_row[0]
                indexed = indexed_materials(pf_filaments)
                file_grams = {}
                for source, filament in (indexed or {}).items():
                    grams = filament.get("used_grams", filament.get("weight_grams"))
                    if type(grams) in (int, float) and math.isfinite(grams) and grams >= 0:
                        file_grams[source + 1] = float(grams)
                if file_grams or (indexed and len(indexed) > 1):
                    complete = len(file_grams) == len(indexed) and (len(mapped_sources) <= 1 or set(file_grams) == mapped_sources)
                    slot_grams = file_grams if complete else {}
            except (ValueError, TypeError):
                pass

    return slot_grams
