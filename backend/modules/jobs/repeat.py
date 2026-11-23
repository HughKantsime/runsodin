"""Build a fresh queued copy after repeat permissions and mapping validation."""
from modules.jobs.models import Job


def verify_repeat_file(expected_hash, payload):
    from fastapi import HTTPException
    from core.print_payload_snapshot import PayloadUnavailable, check_payload_readable, hash_regular_payload
    if payload.blob_state != "present" or not payload.stored_path:
        raise HTTPException(409, "The original print file is no longer available.")
    try:
        if expected_hash is None:
            check_payload_readable(payload.stored_path)
            return
        actual = hash_regular_payload(payload.stored_path)
    except PayloadUnavailable:
        raise HTTPException(409, "The original print file could not be read.") from None
    if actual != expected_hash:
        raise HTTPException(409, "The original print file has changed. Queue the file from the library instead.")


def build_repeated_job(original, actor, mapping, held_for_planned_load):
    return Job(
        model_id=original.model_id,
        print_file_id=original.print_file_id,
        payload_sha256=original.payload_sha256,
        plate_index=original.plate_index,
        ams_mapping=mapping,
        hold=held_for_planned_load,
        charged_to_org_id=original.charged_to_org_id,
        charged_to_user_id=actor.get("id"),
        submitted_by=actor.get("id"),
        item_name=original.item_name,
        quantity=original.quantity,
        status="pending",
        priority=original.priority,
        printer_id=original.printer_id,  # Same printer preference (not a pin)
        # Same target (an explicit pin, model or protocol target) as the original.
        target_type=original.target_type,
        target_filter=original.target_filter,
        duration_hours=original.duration_hours,
        colors_required=original.colors_required,
        filament_type=original.filament_type,
        notes=f"Repeat of job #{original.id}" + (f" - {original.notes}" if original.notes else ""),
        estimated_cost=original.estimated_cost,
        suggested_price=original.suggested_price,
        quantity_on_bed=original.quantity_on_bed,
    )
