"""Validate archived payload options before transport."""
import json
import hmac

def _validated_print_options(job,creds,stored_path,payload_sha256):
    printer_id=job['printer_id']
    if job.get("upload_target_printer_id") not in (None, printer_id):
        return None, "Print file is bound to another printer; no command was sent"
    api_type=creds['api_type']
    print_options = {}
    if job.get("print_file_id") is not None:
        # Revalidate exact payload immediately before any transport operation.
        from core.print_payload import validate_plate
        from fastapi import HTTPException
        expected = str(job.get("payload_sha256") or "").lower()
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            return None, "Archived payload hash is missing or invalid; no command was sent"
        if not hmac.compare_digest(expected,payload_sha256):
            return None, "Archived payload changed; no command was sent"
        try:
            plate = validate_plate(stored_path, job.get("plate_index"), api_type)
        except HTTPException as exc:
            return None, str(exc.detail)
        if api_type == "bambu":
            print_options["plate_num"] = plate
            mapping = job.get("ams_mapping")
            if isinstance(mapping, str):
                try:
                    mapping = json.loads(mapping)
                except ValueError:
                    return None, "Stored AMS mapping is invalid; no command was sent"
            derived = False
            if mapping is None and job.get("ams_mapping") is None:
                from modules.printers.printer_models import bambu_nozzle_count
                if bambu_nozzle_count(creds.get('machine_type') or creds.get('model')) == 2:
                    from modules.printers.services import inspect_ams_payload
                    from modules.printers.dispatch_payload_snapshot import PayloadUnavailable
                    import os
                    try:
                        facts = inspect_ams_payload(stored_path, expected_sha256=payload_sha256,
                            expected_bytes=os.stat(stored_path).st_size, plate_index=job.get('plate_index'))
                        source = facts['source_index']
                        mapping = [-1] * source + [source]
                        derived = True
                    except (ValueError, OSError, PayloadUnavailable):
                        pass  # Unverifiable defaults retain the existing unmapped path.
            if mapping is not None:
                error = _mapping_error(job, mapping, stored_path, payload_sha256, derived=derived)
                if error:
                    if not derived:
                        return None, error
                else:
                    print_options["ams_mapping"] = mapping
        elif job.get("ams_mapping") is not None:
            return None, "AMS mapping requires a Bambu printer; no command was sent"

    return print_options,None


def _mapping_error(job, mapping, stored_path, payload_sha256, *, derived):
    from core.print_payload import validate_mapping
    from fastapi import HTTPException
    # Bambu tray index 0-15, with -1 for unused file sources. AMS HT (tray
    # id 128+) is not a dispatch target; see ams_identity.
    if (not isinstance(mapping, list) or not mapping or len(mapping) > 16
            or any(type(slot) is not int or not -1 <= slot < 16 for slot in mapping)
            or len({slot for slot in mapping if slot != -1}) != sum(slot != -1 for slot in mapping)):
        return "Stored AMS mapping is invalid; no command was sent"
    from core.db import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        slots = {row[0]: row[1] for row in conn.execute(
            text("SELECT slot_number,filament_type FROM filament_slots WHERE printer_id=:id"),
            {"id": job['printer_id']}).fetchall()}
    try:
        validated = validate_mapping([{"source_slot": i + 1, "target_slot": slot + 1}
                          for i, slot in enumerate(mapping) if slot != -1],
                         job.get("filaments_json"), slots, 'bambu')
        if validated != mapping:
            return "Stored AMS mapping differs from file sources; no command was sent"
    except HTTPException as exc:
        return str(exc.detail)
    # Finish the slot transaction before bounded source inspection.
    # Predictably unsupported source shapes must not commit admission.
    from modules.printers.dispatch_ams_gate import preflight_ams_source
    from modules.printers.dispatch_payload_snapshot import PayloadUnavailable
    try:
        preflight_ams_source(job, mapping=mapping, staged_path=stored_path,
                             payload_sha256=payload_sha256, derived=derived)
    except (ValueError, OSError, PayloadUnavailable):
        return "Explicit AMS source or plate is unsupported; no command was sent"
    return None
