"""Sanctioned cross-module printer services without credential exposure."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from modules.printers.kobra_s1 import is_ace_slot
from modules.printers.printer_models import is_h3c_printer, rack_model_identity, rack_printer_refusal
from modules.printers.education_compatibility import evaluate_education_compatibility
from modules.printers.dispatch_support import (
    automatic_dispatch_unsupported_reason,
    dispatch_unsupported_reason,
    is_kobra_s1,
    KOBRA_DISPATCH_DETAIL,
)


def stored_or_file_materials(filaments_json, stored_path):
    from modules.printers.kobra_s1 import stored_or_file_materials as materials
    return materials(filaments_json, stored_path)


def s1_start_problem(db, job):
    """Why this S1 job can't start as it stands, in words for the queue, or None."""
    from core.print_file_binding import job_file_id
    from modules.printers.kobra_s1 import S1Refusal, mapping_vector, read_filaments
    from modules.printers.slot_feed_policy import flagged_mapping_reason
    file_id = job_file_id(db, job.model_id, job.print_file_id)
    stored_path = db.execute(text('SELECT stored_path FROM print_files WHERE id=:id'), {'id': file_id}).scalar() if file_id else None
    if not stored_path:
        return 'The job has no stored print file.'
    try:
        mapping = mapping_vector(job.ams_mapping, read_filaments(stored_path))
    except S1Refusal as refusal:
        return refusal.detail
    except OSError:
        return 'Payload cannot be read'
    flagged = flagged_mapping_reason(db, job.printer_id, mapping)
    return flagged.split(': ', 1)[-1] if flagged else None


def s1_start_facts(db, token, *, job_id=None):
    """Publish evidence recorded for one Kobra S1 attempt, or None."""
    from modules.printers.start_next import load
    from core.controlled_parts import policies
    if job_id is None or policies(db, 'job', job_id):
        return None
    return load(db, 's1_start:' + token)


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


def inspect_ams_payload(path,*,expected_sha256,expected_bytes,plate_index,multi_source=False):
    """Verify an owned immutable snapshot outside approval authority locks.

    Caller first authorizes the exact committed file graph; this service does
    not authorize a pathname. No bytes or filesystem paths are returned.
    """
    import os
    from core.ams_source_inspection import inspect_single_source_ams,inspect_ams_sources,MAX_ARCHIVE_BYTES
    from modules.printers.dispatch_payload_snapshot import stage_payload
    if type(expected_bytes) is not int or expected_bytes <= 0:
        raise ValueError('AMS payload size unavailable')
    if expected_bytes > MAX_ARCHIVE_BYTES:
        raise ValueError(f'AMS payload is too large ({expected_bytes} bytes; limit {MAX_ARCHIVE_BYTES} bytes)')
    with stage_payload(path,expected_sha256) as snapshot:
        with open(snapshot.path,'rb') as source:
            if os.fstat(source.fileno()).st_size != expected_bytes:
                raise ValueError('AMS payload changed')
            # stage_payload already verified the hash while copying bounded chunks.
            verified=(inspect_ams_sources(source,plate_index) if multi_source
                      else inspect_single_source_ams(source,plate_index))
    if multi_source:
        from dataclasses import asdict
        facts = {'payload_sha256':expected_sha256,'storage_bytes':expected_bytes,'plate_index':verified.plate_index,
                 'sources':tuple(asdict(source) for source in verified.sources)}
        if len(verified.sources) == 1:
            facts.update(source_index=verified.sources[0].source_index, material=verified.sources[0].material)
        return facts
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


def resume_moonraker_detection(db, printer, print_job_id, actor, detection_id):
    from modules.printers.moonraker_controls import send_control
    return send_control(db, printer, "resume", actor, expected_print_job_id=print_job_id,
                        detection_id=detection_id, via="vision_dismissal")


def request_observed_task_resume(printer, print_job_id: int) -> str:
    """Caller must hold the detection/printer/observation authority fence.

    Bambu adds monitor-side current observation and paused telemetry admission.
    Acceptance never proves the printer has actually resumed.
    """
    if rack_printer_refusal(printer):
        return 'refused'
    if (printer.api_type or '').lower() == 'bambu':
        from modules.printers.monitor_commands import request_command
        return request_command(printer.id, 'resume', print_job_id=print_job_id)
    if printer.api_type not in ('prusalink', 'octoprint', 'elegoo'):
        return 'refused'  # Never inherit the legacy helper's default Bambu fallback.
    from modules.printers.route_utils import _send_printer_command
    return 'accepted' if _send_printer_command(printer, 'resume_print') else 'unknown'


def configured_identity(printer_id, connection):
    from modules.printers.dispatch import _get_printer_info
    from core.auth import SECRET_KEY
    from core.dispatch_identity import build_identity
    return build_identity(_get_printer_info(printer_id,connection=connection),SECRET_KEY.encode())


def recovery_configuration(connection, evidence):
    from core.auth import SECRET_KEY
    from core.dispatch_recovery_identity import compare
    from modules.printers.dispatch import _get_printer_info
    claim = evidence['claim']
    creds = _get_printer_info(claim['printer_id'], connection=connection)
    return compare(connection, claim, evidence['receipts'], creds, SECRET_KEY.encode())


def configured_recipe_key(printer_id, connection):
    """Read-only stable recipe identity; does not lock or backfill receipts."""
    from modules.printers.dispatch import _get_printer_info
    from core.auth import SECRET_KEY
    from core.dispatch_identity import stable_configuration_key
    return stable_configuration_key(_get_printer_info(printer_id, connection=connection), SECRET_KEY.encode())


def bambu_tray_counts(report, model=None):
    from modules.printers.telemetry.bambu.ams_identity import ams_connection_counts, ams_trays_by_slot
    root = report.get('ams')
    units = root.get('ams') if isinstance(root, dict) else None
    h3c = rack_model_identity(model) is True
    return ams_connection_counts(units, (n for n, _ in ams_trays_by_slot(units, h3c=h3c)), h3c=h3c)


def validate_bambu_slot_count(count):
    from modules.printers.bambu_slots import VALID_BAMBU_COUNTS
    if type(count) is not int or count not in VALID_BAMBU_COUNTS:
        from fastapi import HTTPException
        raise HTTPException(400, 'Choose 1 for an external spool or 4 slots per AMS, up to 16. '
                                 'The printer reports AMS HT trays separately.')


def check_bambu_create_count(values):
    if isinstance(values, dict) and str(values.get('api_type')).lower() == 'bambu':
        if is_h3c_printer(values):
            return {**values, 'slot_count': 9}
        validate_bambu_slot_count(values.get('slot_count', 4))
    return values


def prepare_rack_slot_count(printer, update_data):
    rack = is_h3c_printer({key: update_data.get(key, getattr(printer, key))
                           for key in ('api_type', 'model', 'machine_type')})
    if rack:
        from fastapi import HTTPException
        if update_data.get('slot_count', 9) != 9:
            raise HTTPException(422, 'The H3C has nine rack positions.')
        if printer.slot_count != 9:
            update_data['slot_count'] = 9
    return rack


def create_printer_record(db, **fields):
    """Capacity check and insertion in the caller's transaction, for API/setup."""
    from core.license_capacity import lock_capacity
    from license_manager import check_printer_limit
    from modules.printers.models import Printer
    lock_capacity(db, "printers")
    check_printer_limit(db.query(Printer).count())
    if (fields.get('api_type') or '').lower() == 'bambu':
        from modules.printers.printer_models import BAMBU_BEDS, bambu_model_name
        fields = check_bambu_create_count(fields)
        dims = BAMBU_BEDS.get(bambu_model_name(fields.get('model')))
        if dims and fields.get('bed_x_mm') is None and fields.get('bed_y_mm') is None:
            fields['bed_x_mm'], fields['bed_y_mm'] = dims
    printer = Printer(**fields)
    db.add(printer)
    db.flush()
    return printer


def apply_printer_updates(printer, update_data):
    """Apply prepared values and return the names of fields changed for the audit."""
    from core import crypto
    changed_fields = []
    for field, value in update_data.items():
        old_value = getattr(printer, field)
        if field in ('api_key', 'camera_url') and old_value != value:
            # Re-encrypting an unchanged credential produces different ciphertext.
            changed = crypto.decrypt(old_value) != crypto.decrypt(value)
        else:
            changed = old_value != value
        if changed:
            changed_fields.append(field)
        setattr(printer, field, value)
    if 'model' in changed_fields and rack_model_identity(printer.model, printer.machine_type) is False:
        rows = printer.installed_nozzles
        if isinstance(rows, list) and any(isinstance(row, dict) and row.get('side') == 'rack' for row in rows):
            printer.installed_nozzles = [row for row in rows if not isinstance(row, dict) or row.get('side') != 'rack']
    return changed_fields


def experimental_refusal(db, printer_id, api_type):
    """Service-layer entry for other modules (the scheduler): see support_levels.experimental_refusal."""
    from modules.printers.support_levels import experimental_refusal as _refusal
    return _refusal(db, printer_id, api_type)


def printer_wait_states(db, printers, now, actor):
    """Why work assigned to these printers is not starting, in one read however many printers.

    {printer id: ('offline' | 'busy', the running print's estimated end or None)}; an idle
    printer is absent. Online and printing follow the Home Assistant status rules.
    Estimates from a linked job are withheld unless the caller may see that job.
    """
    from datetime import timedelta
    from sqlalchemy import bindparam
    from modules.printers.routes_integrations import PAUSED_STATES, PRINTING_STATES, is_online
    states, printing = {}, []
    for printer in printers:
        state = (printer.gcode_state or "").strip().upper()
        if not is_online(printer.gcode_state, printer.last_seen, now):
            states[printer.id] = ("offline", None)
        elif state in PRINTING_STATES or state in PAUSED_STATES:
            states[printer.id] = ("busy", None)
            if state in PRINTING_STATES:  # a paused print's remaining time says nothing about its end
                printing.append(printer.id)
    if printing:
        from core.rbac import get_org_scope
        from modules.printers.integration_floor import visible_clauses
        clauses, params = visible_clauses(actor, get_org_scope(actor), controlled=True)
        # Keep the latest print even when hidden: an older visible print is not an ETA fallback.
        sql = ("SELECT p.printer_id, CASE WHEN p.scheduled_job_id IS NULL OR EXISTS "
               "(SELECT 1 FROM jobs WHERE jobs.id = p.scheduled_job_id AND " + " AND ".join(clauses) + ") THEN p.remaining_minutes END "
               "FROM print_jobs p WHERE p.status = 'running' AND p.printer_id IN :ids "
               "ORDER BY p.started_at DESC, p.id DESC")
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- source-owned visibility predicates; every value is bound
        rows = db.execute(text(sql).bindparams(bindparam("ids", expanding=True)), {**params, "ids": printing}).all()
        seen = set()
        for printer_id, remaining in rows:
            if printer_id in seen:
                continue
            seen.add(printer_id)
            try:
                minutes = float(remaining)
            except (TypeError, ValueError):
                continue
            if 0 <= minutes <= 60 * 24 * 30:
                states[printer_id] = ("busy", now + timedelta(minutes=minutes))
    return states


def latest_gate_refusals(db, job_ids, actor):
    """Latest refusal per job; detail requires access to the attempted printer (one read).

    Unlike the print-floor helper, retain the last attempt even after reassignment.
    A missing/deleted printer gives no authority to describe that attempt.
    """
    from sqlalchemy import bindparam
    from core.printer_display_facts import may_read
    from core.rbac import check_org_access
    from modules.printers.integration_floor import refusal_words
    if not job_ids:
        return {}
    rows = db.execute(text(
        "SELECT t.job_id, t.kind, t.code, p.id, p.org_id, p.shared FROM job_timeline_events t "
        "LEFT JOIN printers p ON p.id = t.printer_id "
        "WHERE t.job_id IN :ids AND t.kind IN ('gate_refused','dispatch_uploading','dispatch_starting','dispatch_dispatched','dispatch_failed') "
        "AND t.occurred_at = (SELECT MAX(u.occurred_at) FROM job_timeline_events u "
        "WHERE u.job_id = t.job_id AND u.kind IN ('gate_refused','dispatch_uploading','dispatch_starting','dispatch_dispatched','dispatch_failed')) "
        "ORDER BY t.id"
    ).bindparams(bindparam("ids", expanding=True)), {"ids": list(job_ids)})
    found = {}
    cleared, hidden = set(), set()
    # UUIDs only stabilize wording among tied refusals; they do not order attempts.
    # A tied dispatch clears the refusal, and every tied printer must be visible.
    for job_id, kind, code, printer_id, org_id, shared in rows:
        if kind == "gate_refused":
            visible = printer_id is not None and may_read(actor, "printers") and (check_org_access(actor, org_id) or shared)
            if not visible:
                hidden.add(job_id)
            found[job_id] = (str(code or "").upper(), refusal_words(code) if visible else None)
        else:
            cleared.add(job_id)
    return {job_id: (code, None if job_id in hidden else detail)
            for job_id, (code, detail) in found.items() if job_id not in cleared}


def paused_prints(db, now):
    from modules.printers.integration_floor import paused_prints as load
    return load(db, now)

def automatic_moonraker_pause(engine, printer_id, live_check, detection_id=None):
    from sqlalchemy.orm import Session
    from core.db import concrete_engine
    from modules.printers.models import Printer
    from modules.printers.moonraker_controls import send_control
    with Session(concrete_engine(engine)) as db:
        printer = db.get(Printer, printer_id)
        if printer is None or printer.api_type != "moonraker":
            return "skipped"
        receipt = send_control(db, printer, "pause", {}, live_check=live_check, detection_id=detection_id)
        if receipt.get("reason") == "pending_pause" and detection_id is not None:
            from modules.notifications.services import note_skipped_pause
            note_skipped_pause(db, printer_id, detection_id)
            db.commit()
        return receipt["outcome"]
