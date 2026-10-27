"""Bambu capability preflight and fresh post-upload recheck."""
import time
from threading import Lock
from modules.printers.dispatch_ams_gate import record_refusal
from modules.printers.nozzle_capability import installed_nozzles, compare_nozzles
from modules.printers.dispatch_capability_payload import inspect_payload, geometry_refusal


def create_capability_gate(*, job, creds, staged_path, plate_number, target_handle, previous=None):
    facts = inspect_payload(staged_path, plate_number, creds.get('machine_type') or creds.get('model'))
    lock = Lock()
    prepared = used = False
    ready = False
    acquired_at = None

    def refuse(code, detail):
        return record_refusal(checked, code, detail)

    def prior(operation):
        if previous is None:
            return True
        callback = getattr(previous, 'prepare', None) if operation == 'prepare' else previous
        if callback is None or (callback(deadline=acquired_at + 10) if operation == 'prepare' else callback()) is True:
            return True
        refusal = getattr(previous, 'refusal', None)
        return refuse(getattr(refusal, 'code', 'ams_read_failed'), getattr(refusal, 'detail', 'AMS gate refused'))

    def prepare():
        nonlocal prepared, ready, acquired_at
        with lock:
            if prepared or used:
                return refuse('already_used', 'Capability gate is single use')
            prepared = True
            if not target_handle.held():
                return refuse('target_lock_lost', 'Printer target lock is no longer held')
            from modules.printers.printer_models import bambu_model_name
            required = bambu_model_name(facts.get('printer_model'))
            actual = bambu_model_name(creds.get('machine_type') or creds.get('model'))
            if required and actual and required != actual:
                return refuse('printer_model_mismatch', f'File requires {required}; printer is {actual}')
            detail = geometry_refusal(facts, creds)
            if detail:
                return refuse('bed_geometry_mismatch', detail)
            from modules.printers.dispatch_acquisition import bounded_bambu_report
            # Start the age budget before acquisition, never renew it at return.
            acquired_at = time.monotonic()
            packet = bounded_bambu_report(creds, acquired_at + 10)
            if packet is None:
                return refuse('nozzle_read_failed', 'Fresh printer report could not be verified')
            from modules.printers.printer_models import bambu_nozzle_count
            dual = bambu_nozzle_count(creds.get('machine_type') or creds.get('model')) == 2
            installed = installed_nozzles(packet, dual=dual)
            detail, incomplete = compare_nozzles(facts['requirements'], installed)
            if detail:
                return refuse('nozzle_mismatch', detail)
            if incomplete or facts.get('incomplete') or facts.get('notes'):
                from core.job_timeline import enqueue, event
                try:
                    enqueue(event(job['id'], 'nozzle_unverified', printer_id=job['printer_id'], detail={'geometry_unverified': bool(facts.get('notes'))}))
                except Exception:
                    pass  # Diagnostics cannot turn unknown metadata into a refusal.
            if not prior('prepare'):
                return False
            ready = True
            return True

    def checked():
        nonlocal used
        if not prepared and not prepare():
            return False
        with lock:
            if used:
                return refuse('already_used', 'Capability gate is single use')
            used = True
            if not ready:
                return False
            if not prior('check'):
                return False
            if not target_handle.held():
                return refuse('target_lock_lost', 'Printer target lock is no longer held')
            if acquired_at is None or not 0 <= time.monotonic() - acquired_at < 10:
                return refuse('snapshot_stale', 'Nozzle observation expired before START')
            return True

    checked.refusal = None
    checked.prepare = prepare
    return checked


def static_refusal(path, plate_number, creds):
    """Model and geometry are local facts, checked before creating a claim."""
    from modules.printers.printer_models import bambu_model_name
    from modules.printers.dispatch_ams_gate import GateRefusal
    facts = inspect_payload(path, plate_number, creds.get('machine_type') or creds.get('model'))
    required = bambu_model_name(facts.get('printer_model'))
    actual = bambu_model_name(creds.get('machine_type') or creds.get('model'))
    if required and actual and required != actual:
        return GateRefusal('printer_model_mismatch', f'File requires {required}; printer is {actual}')
    detail = geometry_refusal(facts, creds)
    return GateRefusal('bed_geometry_mismatch', detail) if detail else None
