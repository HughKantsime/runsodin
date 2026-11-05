"""Build a fresh queued copy after repeat permissions and mapping validation."""
from modules.jobs.models import Job


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
