"""Fresh post-upload check for one already-admitted, explicitly mapped attempt."""
from core.material_identity import canonical_reported_material
import copy
import math
import json
import os
import time
from threading import Lock
from dataclasses import dataclass


@dataclass(frozen=True)
class GateRefusal:
    code: str
    detail: str


def record_refusal(gate, code, detail):
    """Best-effort diagnostics must never change a clean refusal outcome."""
    try:
        if getattr(gate, "refusal", None) is None:
            setattr(gate, "refusal", GateRefusal(code, detail))
    except Exception:
        pass
    return False

from core.runtime_identity import read_runtime_identity, revalidate_runtime_identity
from modules.printers.dispatch_admission import recheck_admitted
from modules.printers.telemetry.bambu.ams_configuration import load_ams_configuration
from modules.printers.telemetry.bambu.trusted_ams_acquisition import read_current_selected_ams


def preflight_ams_source(job, *, mapping, staged_path, payload_sha256):
    """Read-only source check. No admission, database transaction or device I/O."""
    from modules.printers.services import inspect_ams_payload
    if (type(mapping) is not list or len(mapping) != 1
            or type(mapping[0]) is not int or not 0 <= mapping[0] < 16
            or job.get('plate_index') != 0):
        raise ValueError('Explicit AMS dispatch requires supported source and plate')
    stored_mapping = job.get('ams_mapping')
    if isinstance(stored_mapping, str):
        stored_mapping = json.loads(stored_mapping)
    if (type(stored_mapping) is not list or stored_mapping != mapping
            or any(type(slot) is not int for slot in stored_mapping)):
        raise ValueError('AMS selection differs from the admitted job')
    facts = inspect_ams_payload(staged_path, expected_sha256=payload_sha256,
                               expected_bytes=os.stat(staged_path).st_size, plate_index=job['plate_index'])
    return facts


def create_ams_start_gate(engine, *, admission_inputs, staged_path, mapping, target_handle):
    """Private orchestrator boundary; callers must already own the admitted attempt.

    No transaction stays open during file inspection or the owned network read.
    Returned callback cannot grant a new claim or renew original evidence.
    """
    captured = dict(admission_inputs)
    for field in ('job', 'creds', 'education_reservation'):
        if field in captured:
            captured[field] = copy.deepcopy(captured[field])
    job = captured['job']
    facts = preflight_ams_source(job, mapping=mapping, staged_path=staged_path,
                                 payload_sha256=captured['payload_sha256'])
    expected_mapping = tuple(mapping)
    slots = (mapping[0] + 1,)
    runtime = read_runtime_identity()
    with engine.connect() as conn:
        configuration = load_ams_configuration(conn, job['printer_id'])
    if configuration.owner_org_id != captured['creds'].get('org_id'):
        raise ValueError('AMS owner changed')

    invocation_lock = Lock()
    prepared = False
    preparation_ok = False
    used = False
    snapshot = None

    def refuse(code, detail):
        return record_refusal(checked, code, detail)

    def runtime_current():
        try:
            revalidate_runtime_identity(runtime)
        except Exception:
            return refuse("runtime_changed", "Runtime identity could not be revalidated")
        return True

    def current():
        if tuple(mapping) != expected_mapping or any(type(slot) is not int for slot in mapping):
            return refuse("mapping_changed", "AMS selection changed")
        if target_handle.held() is not True:
            return refuse("target_lock_lost", "Printer target lock is no longer held")
        if not runtime_current():
            return False
        admission = recheck_admitted(engine, **captured)
        if not admission.admitted:
            return refuse("admission_changed", f"Dispatch admission is no longer valid: {getattr(admission, 'reason', 'Admission recheck refused')}")
        with engine.connect() as conn:
            present = load_ams_configuration(conn, job['printer_id'])
        if present.owner_org_id != configuration.owner_org_id:
            return refuse("ams_owner_changed", "AMS owner changed")
        if present != configuration:
            return refuse("ams_configuration_changed", "AMS configuration changed")
        return True

    def acquire(deadline=None):
        nonlocal snapshot
        if not current():
            return False
        result = read_current_selected_ams(configuration.transport,
            configuration_key=configuration.configuration_key,
            owner_org_id=configuration.owner_org_id, selected_slots=slots,
            timeout=max(0.0, min(10.0, deadline - time.monotonic())) if deadline is not None else 10.0, operation_id=captured['token'])
        if result.success is not True:
            return refuse("ams_read_failed", "Fresh AMS read failed")
        if result.cleanup_confirmed is not True:
            return refuse("ams_read_cleanup_unconfirmed", "AMS read cleanup was not confirmed")
        if (result.runtime_incarnation != runtime.incarnation or result.clock_domain != runtime.clock_domain):
            return refuse("ams_read_runtime_mismatch", "AMS read belongs to another runtime")
        snapshot = result.snapshot
        return valid()

    def valid():
        if snapshot is None:
            return refuse("snapshot_missing", "Fresh AMS snapshot is missing")
        if (snapshot.incarnation != captured['token'] or snapshot.configuration_key != configuration.configuration_key
                or snapshot.owner_org_id != configuration.owner_org_id):
            return refuse("snapshot_identity_mismatch", "AMS snapshot identity does not match this attempt")
        if snapshot.idle_without_error is not True:
            return refuse("printer_not_idle_or_error", f"gcode_state={getattr(snapshot, 'gcode_state', None)}; print_error={getattr(snapshot, 'print_error', None)}; active_hms_codes={getattr(snapshot, 'active_hms_codes', None)}")
        if len(snapshot.trays) != 1:
            return refuse("tray_count", f"Selected tray count={len(snapshot.trays)}; expected=1")
        tray = snapshot.trays[0]
        if tray.slot_number != slots[0]:
            return refuse("tray_slot_mismatch", f"Reported slot={tray.slot_number}; expected slot={slots[0]}")
        if tray.material_state != 'reported':
            if tray.material_state == 'explicit_empty':
                return refuse("tray_material_unreported", f"AMS slot {tray.slot_number} is empty: load a spool and set the filament type on the printer")
            return refuse("tray_material_unreported", f"AMS slot {tray.slot_number}: set the filament type on the printer")
        if tray.material != facts['material']:
            return refuse("material_mismatch", f"Reported material={canonical_reported_material(tray.material)}; required material={canonical_reported_material(facts['material'])}")
        if tray.mechanically_ready is not None:
            return refuse("tray_not_mechanically_ready", "Selected tray mechanical readiness is unsupported")
        if not current():
            return False
        if not runtime_current():
            return False
        if type(snapshot.received_monotonic) not in (int, float) or not math.isfinite(snapshot.received_monotonic):
            return refuse("snapshot_stale", "Snapshot age seconds=invalid")
        age = time.monotonic() - snapshot.received_monotonic
        if not (type(age) in (int, float) and math.isfinite(age) and 0 <= age < 10):
            return refuse("snapshot_stale", f"Snapshot age seconds={format(age, '.1f') if math.isfinite(age) else 'invalid'}")
        if target_handle.held() is not True:
            return refuse("target_lock_lost", "Printer target lock is no longer held")
        return True

    def prepare(*, deadline=None):
        nonlocal prepared, preparation_ok
        with invocation_lock:
            if prepared or used:
                return refuse("already_used", "AMS gate preparation is single use")
            # Never reacquire after failure, or renew a proof's receipt time.
            prepared = True
            try:
                preparation_ok = acquire(deadline)
                return preparation_ok
            except Exception as exc:
                return refuse("exception:" + type(exc).__name__, "AMS gate raised an exception")

    def checked():
        nonlocal prepared, preparation_ok, used
        with invocation_lock:
            if used:
                return refuse("already_used", "AMS gate is single use")
            used = True
            try:
                # Preserve the private direct-call contract for callers/tests.
                # Dispatch prepares before connecting and takes only this cheap
                # one-shot final check at the command boundary.
                if not prepared:
                    prepared = True
                    preparation_ok = acquire()
                return preparation_ok is True and valid()
            except Exception as exc:
                return refuse("exception:" + type(exc).__name__, "AMS gate raised an exception")

    checked.refusal = None
    checked.prepare = prepare
    return checked
