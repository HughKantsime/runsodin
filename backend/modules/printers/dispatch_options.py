"""Validate archived payload options before transport."""
import json
import hmac

class ValidatedPrintOptions(dict):
    """Transport options with private source evidence, never a wire option."""

    source_facts = None


def _validated_print_options(job,creds,stored_path,payload_sha256):
    printer_id=job['printer_id']
    if job.get("upload_target_printer_id") not in (None, printer_id):
        return None, "Print file is bound to another printer; no command was sent"
    api_type=creds['api_type']
    from modules.printers.dispatch_support import is_kobra_s1
    if is_kobra_s1(creds):
        from modules.printers.s1_dispatch import options
        from modules.printers.kobra_s1 import S1Refusal
        # Archived hash/plate validation still applies before protocol planning.
        if job.get('print_file_id') is not None:
            if not hmac.compare_digest(str(job.get('payload_sha256') or '').lower(), payload_sha256):
                return None, 'Archived payload changed; no command was sent'
            from core.print_payload import validate_plate
            from fastapi import HTTPException
            try:
                validate_plate(stored_path, job.get('plate_index'), api_type)
            except HTTPException as exc:
                return None, str(exc.detail)
        try:
            result = options(job, stored_path)
            from core.db import engine
            from modules.printers.slot_feed_policy import flagged_mapping_reason
            with engine.connect() as conn:
                reason = flagged_mapping_reason(conn, printer_id, result['ams_mapping'])
            return (None, reason) if reason else (result, None)
        except (S1Refusal, OSError) as exc:
            return None, str(exc) if isinstance(exc, S1Refusal) else 's1_file_unavailable: Payload cannot be read'
    print_options = ValidatedPrintOptions()
    from modules.printers.printer_models import identified_h3c
    h3c = identified_h3c(creds)
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
            if requires_mapping and not h3c and job.get("ams_mapping") is None:
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
            facts = None
            if requires_mapping and (h3c or job.get('ams_mapping') is None):
                from modules.printers.services import inspect_ams_payload
                from modules.printers.dispatch_payload_snapshot import PayloadUnavailable
                import os
                try:
                    facts = inspect_ams_payload(stored_path, expected_sha256=payload_sha256,
                        expected_bytes=os.stat(stored_path).st_size, plate_index=plate_index,
                        **({'multi_source': True} if h3c else {}))
                    derived = job.get('ams_mapping') is None
                    if h3c:
                        from modules.printers.dispatch_ams_gate import rack_sources
                        sources = rack_sources(job, facts)
                        from core.db import engine
                        from core.h3c_plate import different_positions_enabled
                        from modules.printers.h3c_mapping import onboard_inventory, rack_candidates, derive_mapping, planned_mapping
                        with engine.connect() as conn:
                            trays = onboard_inventory(conn, printer_id)
                            allow_different = derived and different_positions_enabled(conn)
                        if derived:
                            mapping = (derive_mapping(sources, rack_candidates(sources, trays, creds.get('installed_nozzles')))
                                       if allow_different else planned_mapping(sources))
                        facts['inventory_colors'] = {t['slot']: t['color_hex'] for t in trays}
                    else:
                        source = facts['source_index']
                        mapping = [-1] * source + [source]
                except (ValueError, OSError, PayloadUnavailable) as exc:
                    if h3c:
                        from core.h3c_plate import RackMessage
                        if exc.args and isinstance(exc.args[0], RackMessage):
                            return None, exc.args[0]
                        return None, str(exc) if isinstance(exc, ValueError) else 'H3C plate inspection unavailable.'
                    return None, _missing_mapping(job)
            if mapping is None and requires_mapping:
                return None, _missing_mapping(job)
            if mapping is not None:
                error = _mapping_error(job, mapping, rack_sources=facts['sources'] if h3c else (),
                                       installed=creds.get('installed_nozzles'))
                if error:
                    return None, _missing_mapping(job) if derived and not h3c else error
                # No transaction remains open during inspection. Derived mappings
                # already inspected these exact bytes; validate their facts once.
                from modules.printers.dispatch_ams_gate import preflight_ams_source
                from modules.printers.dispatch_payload_snapshot import PayloadUnavailable
                try:
                    print_options.source_facts = preflight_ams_source(job, mapping=mapping,
                        staged_path=stored_path, payload_sha256=payload_sha256,
                        derived=derived, inspected_facts=facts, h3c=h3c)
                except (ValueError, OSError, PayloadUnavailable) as exc:
                    detail = str(exc) if isinstance(exc, ValueError) else 'Payload inspection unavailable'
                    if h3c:
                        return None, detail
                    error = f"Explicit AMS source or plate is unsupported: {detail}; no command was sent"
                    return None, _missing_mapping(job) if derived and not h3c else error
                print_options["ams_mapping"] = mapping
        elif job.get("ams_mapping") is not None:
            return None, "AMS mapping requires a Bambu printer; no command was sent"

    return print_options,None


def _mapping_error(job, mapping, *, slot_details=False, rack_sources=(), installed=None):
    from core.print_payload import MappingMaterialMismatch, MappingSlotUnavailable, validate_mapping
    from fastapi import HTTPException
    # Stored targets are physical slot minus one; -1 is an unused file source.
    if (not isinstance(mapping, list) or not mapping or len(mapping) > 16
            or any(type(slot) is not int or slot < -1 or (not rack_sources and slot >= 24) for slot in mapping)):
        return "Stored AMS mapping is invalid; no command was sent"
    if rack_sources:
        from modules.printers.nozzle_capability import rack_tool_refusal
        detail = rack_tool_refusal(rack_sources, mapping, installed)
        if detail:
            return detail
    from core.db import engine
    from core.ams_mapping import mapping_materials
    from modules.printers.slot_feed_policy import flagged_mapping_reason
    with engine.connect() as conn:
        slots = mapping_materials(conn, job['printer_id'], 'bambu')
        reason = flagged_mapping_reason(conn, job['printer_id'], mapping)
        if rack_sources:
            from modules.printers.h3c_mapping import material_refusal
            from core.h3c_plate import position_gate
            for source in rack_sources:
                position = mapping[source['source_index']] + 1
                detail = material_refusal(source, position, slots.get(position))
                if detail:
                    return detail
            reason = reason or position_gate(conn, rack_sources, mapping)
    if reason:
        return reason
    try:
        validated = validate_mapping([{"source_slot": i + 1, "target_slot": slot + 1}
                          for i, slot in enumerate(mapping) if slot != -1],
                         job.get("filaments_json"), slots, 'bambu')
        if validated != mapping:
            return "Stored AMS mapping differs from file sources; no command was sent"
    except HTTPException as exc:
        if isinstance(exc, (MappingMaterialMismatch, MappingSlotUnavailable)):
            if slot_details:
                return exc
            return f'{exc.detail} (AMS slot {exc.target_slot})'
        return str(exc.detail)
    return None


def _requires_ams_mapping(creds):
    from modules.printers.printer_models import identified_h3c
    # One configured slot normally means external spool only; HT units are separate.
    return creds.get('api_type') == 'bambu' and (
        identified_h3c(creds) or (creds.get('slot_count') or 1) > 1
        or any(17 <= slot[0] <= 24 for slot in creds.get('filament_slots', [])))


def _missing_mapping(job):
    """Suggestions are diagnostics only, never transport options or job writes."""
    from core.ams_mapping import preview_for_slots, selectable_slots
    from core.db import engine
    from sqlalchemy import text
    from modules.printers import dispatch
    from modules.printers.dispatch_ams_gate import GateRefusal
    detail = 'No AMS mapping: choose the tray for each filament.'
    try:
        with engine.connect() as conn:
            available = [dict(row) for row in conn.execute(text(
                'SELECT slot_number AS slot,color,color_hex,filament_type AS material,do_not_feed '
                'FROM filament_slots WHERE printer_id=:id ORDER BY slot_number'),
                {'id': job['printer_id']}).mappings()]
            available = selectable_slots(conn, job['printer_id'], 'bambu', available)
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
        dispatch._notify_start_outcome(job, 'gate_refused', gate_refusal=refusal, message=detail)
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
