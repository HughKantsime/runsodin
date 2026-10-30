"""Validate archived payload options before transport."""
import json
import hmac

def _validated_print_options(job,creds,stored_path,payload_sha256):
    printer_id=job['printer_id']
    if job.get("upload_target_printer_id") not in (None, printer_id):
        return None, "Print file is bound to another printer; no command was sent"
    api_type=creds['api_type']
    print_options = {}
    requires_mapping = _requires_ams_mapping(creds)
    archived = job.get("print_file_id") is not None
    if archived or requires_mapping:
        # Revalidate exact payload immediately before any transport operation.
        from core.print_payload import validate_plate
        from fastapi import HTTPException
        if archived:
            expected = str(job.get("payload_sha256") or "").lower()
            if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
                return None, "Archived payload hash is missing or invalid; no command was sent"
            if not hmac.compare_digest(expected,payload_sha256):
                return None, "Archived payload changed; no command was sent"
        plate_index = _ams_plate_index(job) if requires_mapping else job.get("plate_index")
        try:
            plate = validate_plate(stored_path, plate_index, api_type)
        except HTTPException as exc:
            if requires_mapping and job.get("ams_mapping") is None:
                return None, _missing_mapping(job)
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
                if requires_mapping:
                    from modules.printers.services import inspect_ams_payload
                    from modules.printers.dispatch_payload_snapshot import PayloadUnavailable
                    import os
                    try:
                        facts = inspect_ams_payload(stored_path, expected_sha256=payload_sha256,
                            expected_bytes=os.stat(stored_path).st_size, plate_index=plate_index)
                        source = facts['source_index']
                        mapping = [-1] * source + [source]
                        derived = True
                    except (ValueError, OSError, PayloadUnavailable):
                        return None, _missing_mapping(job)
            if mapping is None and requires_mapping:
                return None, _missing_mapping(job)
            if mapping is not None:
                error = _mapping_error(job, mapping, stored_path, payload_sha256, derived=derived)
                if error:
                    return None, _missing_mapping(job) if derived else error
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
            or any(type(slot) is not int or not -1 <= slot < 16 for slot in mapping)):
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
    except (ValueError, OSError, PayloadUnavailable) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else 'Payload inspection unavailable'
        return f"Explicit AMS source or plate is unsupported: {detail}; no command was sent"
    return None


def _requires_ams_mapping(creds):
    # The printer UI defines one slot as external spool only. HT inventory is
    # AMS too, but retains its existing unsupported-target validation.
    return creds.get('api_type') == 'bambu' and (
        (creds.get('slot_count') or 1) > 1
        or any(17 <= slot[0] <= 24 for slot in creds.get('filament_slots', [])))


def _missing_mapping(job):
    """Suggestions are diagnostics only, never transport options or job writes."""
    from core.ams_mapping import preview_for_slots
    from core.db import engine
    from sqlalchemy import text
    from modules.printers import dispatch
    from modules.printers.dispatch_ams_gate import GateRefusal
    detail = 'No AMS mapping: choose the tray for each filament.'
    try:
        with engine.connect() as conn:
            available = [dict(row) for row in conn.execute(text(
                'SELECT slot_number AS slot,color,color_hex,filament_type AS material '
                'FROM filament_slots WHERE printer_id=:id ORDER BY slot_number'),
                {'id': job['printer_id']}).mappings()]
        preview = preview_for_slots(job.get('filaments_json'), available)
        choices = ', '.join(f"{item['source_slot']}→{item['target_slot']} ({item['confidence']})"
                            for item in preview['suggested_mapping'])
        detail += f' AMS preview suggestion: {choices or "no matching trays"}.'
    except Exception:
        detail += ' AMS preview suggestion unavailable.'
    detail += ' Review and save an explicit AMS mapping before retrying; no upload or start was sent.'
    refusal = GateRefusal('ams_mapping_required', detail)
    # Diagnostics must not turn a known no-upload refusal into an uncertain one.
    try:
        dispatch._notify_start_outcome(job, 'adapter_rejected', gate_refusal=refusal, message=detail)
    except Exception:
        pass
    try:
        dispatch._ws(job['id'], 'failed', f'{refusal.code}: {detail}')
    except Exception:
        pass
    dispatch.log.warning('[dispatch] job_id=%s gate_refusal_code=%s: %s', job['id'], refusal.code, detail)
    return f'{refusal.code}: {detail}'


def _ams_plate_index(job):
    # Legacy model-selected jobs used the first plate before archive binding.
    # Keep that selection, but subject its bytes to the same source inspection.
    if job.get('print_file_id') is None and job.get('plate_index') is None:
        return 0
    return job.get('plate_index')
