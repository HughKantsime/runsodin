"""Fresh post-upload check for one already-admitted, explicitly mapped attempt."""
import copy
import math
import json
import os
import time
from threading import Lock

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
    used = False
    def checked():
        nonlocal used
        with invocation_lock:
            if used:
                return False
            used = True
        try:
            if tuple(mapping) != expected_mapping or any(type(slot) is not int for slot in mapping):
                return False
            if target_handle.held() is not True:
                return False
            revalidate_runtime_identity(runtime)
            if not recheck_admitted(engine, **captured).admitted:
                return False
            with engine.connect() as conn:
                before = load_ams_configuration(conn, job['printer_id'])
            if before != configuration:
                return False
            result = read_current_selected_ams(configuration.transport,
                configuration_key=configuration.configuration_key,
                owner_org_id=configuration.owner_org_id, selected_slots=slots,
                timeout=10.0, operation_id=captured['token'])
            if (result.success is not True or result.cleanup_confirmed is not True
                    or result.runtime_incarnation != runtime.incarnation
                    or result.clock_domain != runtime.clock_domain):
                return False
            snapshot = result.snapshot
            if (snapshot is None or snapshot.incarnation != captured['token']
                    or snapshot.configuration_key != configuration.configuration_key
                    or snapshot.owner_org_id != configuration.owner_org_id
                    or snapshot.idle_without_error is not True
                    or len(snapshot.trays) != 1):
                return False
            tray = snapshot.trays[0]
            if (tray.slot_number != slots[0] or tray.material_state != 'reported'
                    or tray.material != facts['material'] or tray.mechanically_ready is not None):
                return False
            revalidate_runtime_identity(runtime)
            with engine.connect() as conn:
                after = load_ams_configuration(conn, job['printer_id'])
            if after != configuration or not recheck_admitted(engine, **captured).admitted:
                return False
            revalidate_runtime_identity(runtime)
            if tuple(mapping) != expected_mapping or any(type(slot) is not int for slot in mapping):
                return False
            if type(snapshot.received_monotonic) not in (int, float) or not math.isfinite(snapshot.received_monotonic):
                return False
            age = time.monotonic() - snapshot.received_monotonic
            return (type(age) in (int, float) and math.isfinite(age) and 0 <= age < 10
                    and target_handle.held() is True)
        except Exception:
            return False
    return checked
