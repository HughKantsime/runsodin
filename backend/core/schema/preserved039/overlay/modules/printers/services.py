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


class _OwnedAMSRead:
    """Opaque service-owned operation; never serialize transport credentials."""
    def __init__(self,started,runtime,configuration):
        self._started=started
        self._runtime=runtime
        self._configuration=configuration
    def __repr__(self):return '_OwnedAMSRead(<private operation>)'


def begin_ams_read(db,*,printer_id,expected_org_id):
    from core.runtime_identity import read_runtime_identity
    from modules.printers.telemetry.bambu.ams_configuration import load_ams_configuration
    from modules.printers.telemetry.bambu.ams_evidence_transactions import _begin_read
    if type(expected_org_id) is not int or expected_org_id<=0:raise ValueError('AMS owner unavailable')
    runtime=read_runtime_identity()
    started=_begin_read(db,printer_id=printer_id,runtime=runtime)
    configuration=load_ams_configuration(db,printer_id)
    if (configuration.owner_org_id!=expected_org_id
            or configuration.owner_org_id!=started.owner_org_id
            or configuration.configuration_key!=started.configuration_key):
        raise ValueError('AMS owner unavailable')
    return _OwnedAMSRead(started,runtime,configuration)


def acquire_ams_report(operation,*,selected_slots):
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import read_current_selected_ams
    if type(operation) is not _OwnedAMSRead:raise ValueError('AMS operation unavailable')
    configuration=operation._configuration
    return read_current_selected_ams(configuration.transport,configuration_key=configuration.configuration_key,
        owner_org_id=configuration.owner_org_id,selected_slots=selected_slots,timeout=10.0,
        operation_id=operation._started.generation_id)


def persist_ams_report(db,*,operation,result,selected_slots):
    from modules.printers.telemetry.bambu.ams_evidence_transactions import _freeze_read_result,_persist_report
    if type(operation) is not _OwnedAMSRead:raise ValueError('AMS operation unavailable')
    pending=_freeze_read_result(db,started=operation._started,result=result,runtime=operation._runtime,
                                selected_slots=selected_slots)
    sequence=_persist_report(db,pending=pending,runtime=operation._runtime)
    return {'generation_id':operation._started.generation_id,'packet_sequence':sequence,
        'received_at':pending.received_at.isoformat(),
        'trays':[{'slot_number':fact.slot_number,'material':fact.material,
                  'material_state':fact.material_state,'mechanically_ready':None} for fact in pending.facts]}


def retire_ams_read(db,*,operation):
    from modules.printers.telemetry.bambu.ams_evidence_transactions import _fail_owned_read
    if type(operation) is not _OwnedAMSRead:raise ValueError('AMS operation unavailable')
    return _fail_owned_read(db,started=operation._started,runtime=operation._runtime)


def inspect_ams_payload(path,*,expected_sha256,expected_bytes,plate_index):
    """Verify an owned immutable snapshot outside approval authority locks.

    Caller first authorizes the exact committed file graph; this service does
    not authorize a pathname. No bytes or filesystem paths are returned.
    """
    import hashlib,hmac
    from core.ams_source_inspection import inspect_single_source_ams,MAX_ARCHIVE_BYTES
    from modules.printers.dispatch_payload_snapshot import stage_payload
    if type(expected_bytes) is not int or not 0<expected_bytes<=MAX_ARCHIVE_BYTES:
        raise ValueError('AMS payload size unavailable')
    with stage_payload(path,expected_sha256) as snapshot:
        with open(snapshot.path,'rb') as source:
            payload=source.read(MAX_ARCHIVE_BYTES+1)
        if len(payload)!=expected_bytes or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(),expected_sha256):
            raise ValueError('AMS payload changed')
        verified=inspect_single_source_ams(payload,plate_index)
    return {'payload_sha256':expected_sha256,'storage_bytes':expected_bytes,'plate_index':verified.plate_index,
            'source_index':verified.source_index,'material':verified.material}


def validate_current_ams_selection(db,*,printer_id,expected_org_id,generation_id,mapping,verified_payload):
    """Approval-only evidence consumption; never command admission or IO."""
    import re
    from core.runtime_identity import read_runtime_identity,revalidate_runtime_identity
    from core.material_identity import canonical_reported_material
    from core.ams_evidence_schema import generations,packets,trays
    from modules.printers.telemetry.bambu.ams_configuration import load_ams_configuration
    from modules.printers.telemetry.bambu.ams_evidence_transactions import _lock_printer,_current,_instant
    from modules.printers.telemetry.bambu.ams_database_clock import sample_database_clock
    try:
        if (type(expected_org_id) is not int or expected_org_id<=0
                or type(generation_id) is not str or not re.fullmatch('[0-9a-f]{32}',generation_id)):raise ValueError()
        if (type(verified_payload) is not dict or set(verified_payload)!={'payload_sha256','storage_bytes','plate_index','source_index','material'}
                or type(verified_payload['payload_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',verified_payload['payload_sha256'])
                or type(verified_payload['storage_bytes']) is not int or not 0<verified_payload['storage_bytes']<=64*1024*1024
                or type(verified_payload['material']) is not str or canonical_reported_material(verified_payload['material'])!=verified_payload['material']):raise ValueError()
        if (type(mapping) is not list or len(mapping)!=1 or type(mapping[0]) is not dict
                or set(mapping[0])!={'source_slot','target_slot'}
                or type(mapping[0]['source_slot']) is not int or mapping[0]['source_slot']!=0
                or type(mapping[0]['target_slot']) is not int or not 1<=mapping[0]['target_slot']<=16
                or type(verified_payload.get('source_index')) is not int or verified_payload.get('source_index')!=0
                or type(verified_payload.get('plate_index')) is not int or verified_payload.get('plate_index')!=0):raise ValueError()
        runtime=read_runtime_identity()
        _lock_printer(db,printer_id)
        configured=load_ams_configuration(db,printer_id)
        pointer=_current(db,printer_id)
        if (configured.owner_org_id!=expected_org_id or pointer is None
                or pointer['generation_id']!=generation_id or pointer['runtime_incarnation']!=runtime.incarnation):raise ValueError()
        generation=db.execute(generations.select().where(generations.c.generation_id==generation_id).with_for_update(nowait=True)).mappings().one()
        if (generation['state']!='ready' or generation['printer_id']!=printer_id
                or generation['owner_org_id']!=expected_org_id or generation['runtime_incarnation']!=runtime.incarnation
                or generation['configuration_key']!=configured.configuration_key):raise ValueError()
        packet=db.execute(packets.select().where(packets.c.generation_id==generation_id)
            .order_by(packets.c.packet_sequence.desc()).limit(1)).mappings().one()
        now=sample_database_clock(db).database_now
        received=_instant(packet['received_at'])
        if (packet['receipt_provenance']!='db-clock-estimate-v1'
                or received<_instant(generation['started_at']) or received>_instant(generation['completed_at'])
                or not 0<=(now-received).total_seconds()<120):raise ValueError()
        target=mapping[0]['target_slot']
        fact=db.execute(trays.select().where(trays.c.packet_sequence==packet['packet_sequence'],
                                             trays.c.slot_number==target)).mappings().one()
        if (fact['material_state']!='reported' or fact['material']!=verified_payload.get('material')
                or fact['mechanically_ready'] is not None or fact['slot_number']!=fact['unit_id']*4+fact['tray_id']+1):raise ValueError()
        revalidate_runtime_identity(runtime)
        return [target-1]
    except Exception:
        raise ValueError('AMS selection is unavailable or changed; refresh required') from None


def request_observed_task_resume(printer, print_job_id: int) -> str:
    """Caller must hold the detection/printer/observation authority fence.

    Bambu adds monitor-side current observation and paused telemetry admission.
    Acceptance never proves the printer has actually resumed.
    """
    if (printer.api_type or '').lower() == 'bambu':
        from modules.printers.monitor_commands import request_command
        return request_command(printer.id, 'resume', print_job_id=print_job_id)
    if printer.api_type not in ('moonraker', 'prusalink', 'elegoo'):
        return 'refused'  # Never inherit the legacy helper's default Bambu fallback.
    from modules.printers.route_utils import _send_printer_command
    return 'accepted' if _send_printer_command(printer, 'resume_print') else 'unknown'
