"""Explicit S1 upload/start inside the existing claim and sender boundaries."""
import hashlib
import time
import uuid
from modules.printers.kobra_s1 import DECOY_FILENAME, S1Refusal, build_payload, read_filaments, mapping_vector, verify_mapping
from modules.printers.dispatch_ams_gate import record_refusal


def options(job, path):
    filaments = read_filaments(path)
    from modules.printers.dispatch_options import ValidatedPrintOptions
    return ValidatedPrintOptions(ams_mapping=mapping_vector(job.get('ams_mapping'), filaments), s1_filaments=filaments)


def create_start_gate(engine, *, admission_inputs, target_handle, **unused):
    from modules.printers.dispatch_admission import recheck_admitted
    def check_authority():
        if not target_handle.held():
            return record_refusal(gate, 's1_target_changed', 'Configured target ownership changed')
        result = recheck_admitted(engine, **admission_inputs)
        if not result.admitted:
            code, _, detail = result.reason.partition(': ')
            return record_refusal(gate, code, detail or result.reason)
        return True
    def gate():
        return check_authority()
    # Recheck admission and claim ownership without consuming the single-use
    # outer license gate or recording real-start evidence.
    gate.preflight = check_authority
    gate.prepare = lambda: target_handle.held()
    gate.refusal = None
    gate.clean_no_start = False
    gate.engine = engine
    gate.job = admission_inputs['job']
    gate.token = admission_inputs['token']
    return gate


def _staged_identity(path):
    digest = hashlib.md5(usedforsecurity=False)
    size = 0
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(128 * 1024), b''):
            digest.update(block)
            size += len(block)
    return dict(md5=digest.hexdigest(), size=size)


def _fresh(printer):
    status = printer.get_status()
    if status.internal_state != 'IDLE' or status.raw_print_state not in ('standby', 'complete', 'cancelled'):
        raise S1Refusal('s1_printer_not_ready', 'S1 must be idle and ready')
    if status.raw_data.get('webhooks', {}).get('state') != 'ready':
        raise S1Refusal('s1_printer_not_ready', 'Klipper is not ready')
    if status.ace_status is None:
        raise S1Refusal('s1_ace_unknown', 'ACE state is unavailable')
    return status.ace_status


PREFLIGHT_MAX_DECOYS = 3
PREFLIGHT_RETRY_SECONDS = 25.0


def _decoy_file_absent(printer):
    from urllib.error import HTTPError
    try:
        printer.device_file_identity(DECOY_FILENAME)
    except HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return
    except Exception:
        pass
    # Invalid headers, permission errors and incomplete reads do not prove absence.
    raise S1Refusal('s1_preflight_file_not_absent', 'The printer could not confirm that the preflight file is missing. No print was started.')


def _record_preflight(authority, facts):
    from contextlib import nullcontext
    from sqlalchemy.orm import Session
    from core.audit import log_audit
    from core.job_timeline import enqueue, event
    from modules.printers.dispatch_background import audit_actor
    actor = audit_actor()
    with Session(authority.engine) as db:
        with actor(db) if actor else nullcontext():
            log_audit(db, 'job.dispatch_preflight', 'job', authority.job['id'],
                      dict(printer_id=authority.job['printer_id'], **facts))
            db.commit()
    # Timeline kinds are schema-constrained; keep preflight facts on the existing
    # start-preparation event rather than requiring a migration for a new kind.
    enqueue(event(authority.job['id'], 'dispatch_starting', printer_id=authority.job['printer_id'],
                  detail=dict(preflight_result=facts['result'], decoys_sent=facts['decoys_sent'], seconds=facts['seconds'])))


def _preflight(printer, adapter, filaments, slots, mapping, authority):
    from modules.printers.maintenance_gate import check_command, MaintenanceHold
    started = time.monotonic()
    facts = dict(result='refused', decoys_sent=0, seconds=0.0)
    def before_decoy():
        if authority.preflight() is not True:
            refusal = authority.refusal
            raise S1Refusal(refusal.code, refusal.detail)
        fresh = _fresh(printer)
        verify_mapping(filaments, fresh['slots'], mapping)
        if any(fresh['slots'][n] != slots[n] for n in mapping):
            raise S1Refusal('s1_slot_changed', 'The selected filament changed. Check the mapping, then try again.')
        _decoy_file_absent(printer)
        # File checks can take time; idle and maintenance must still hold at send.
        _fresh(printer)
        if authority.preflight() is not True:
            refusal = authority.refusal
            raise S1Refusal(refusal.code, refusal.detail)
        try:
            check_command()
        except MaintenanceHold as exc:
            raise S1Refusal('printer_maintenance_hold', str(exc)) from None
        facts['decoys_sent'] += 1
    try:
        for attempt in range(PREFLIGHT_MAX_DECOYS):
            if attempt:
                time.sleep(PREFLIGHT_RETRY_SECONDS)
            decoy = build_payload(dict(path=DECOY_FILENAME, md5='0' * 32, size=0, filaments=filaments),
                                  slots, mapping, int(time.time() * 1000), str(uuid.uuid4()), decoy=True)
            result = adapter.send_decoy(decoy, before_publish=before_decoy)
            if result.get('processed') is True:
                facts['result'] = 'passed'
                return
        raise S1Refusal('s1_start_service_not_responding', "The printer's start service did not respond. Restart it on the printer, then try again.")
    except S1Refusal:
        raise
    except Exception:
        raise S1Refusal('s1_preflight_unavailable', 'The printer start check could not be completed. No print was started; try again later.') from None
    finally:
        facts['seconds'] = round(time.monotonic() - started, 3)
        try:
            _record_preflight(authority, facts)
        except Exception:
            raise S1Refusal('s1_preflight_record_failed', 'The printer start check could not be recorded. No print was started; try again later.') from None


def dispatch(job_id, path, remote, creds, print_options, gate):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    from modules.printers.adapters.anycubic_lan import AnycubicLANPrinter
    from modules.printers import dispatch as common
    from modules.printers.dispatch_support import s1_sending_enabled, S1_SENDING_OFF_DETAIL
    if not s1_sending_enabled():   # last line of defence; admission refuses long before this
        return False, common.NoStartMessage('printer_dispatch_unsupported: ' + S1_SENDING_OFF_DETAIL)
    authority = getattr(print_options, 's1_gate', gate)
    if not callable(gate) or not hasattr(authority, 'engine'):
        return False, 's1_gate_required: S1 dispatch requires admitted authority'
    printer = MoonrakerPrinter(creds['ip'], creds['port'], creds.get('api_key', ''),
                               model=creds.get('model'), machine_type=creds.get('machine_type'))
    publish_entered = False
    published_at = None
    mapping, filaments = print_options['ams_mapping'], print_options['s1_filaments']
    try:
        ace = _fresh(printer)
        verify_mapping(filaments, ace['slots'], mapping)
        if not printer.upload_file(path, remote):
            code = 'printer_busy' if printer.last_upload_busy else 's1_upload_failed'
            detail = 'Printer is busy; retry later' if printer.last_upload_busy else 'Moonraker upload failed before start'
            raise S1Refusal(code, detail)
        try:
            file = printer.device_file_identity(remote)
        except S1Refusal:
            raise
        except Exception:
            raise S1Refusal('s1_device_file_unavailable', 'Device file identity could not be read') from None
        staged = _staged_identity(path)
        if file['size'] < staged['size']:
            raise S1Refusal('s1_device_file_incomplete', 'Device copy is smaller than the uploaded file')
        if file['filaments'] != filaments:
            raise S1Refusal('s1_file_changed', 'Device filament header differs from the admitted payload')
        payload = build_payload(file, ace['slots'], mapping, int(time.time() * 1000), str(uuid.uuid4()))
        adapter = AnycubicLANPrinter(creds['ip'])
        try:
            _preflight(printer, adapter, filaments, ace['slots'], mapping, authority)
        except S1Refusal as exc:
            record_refusal(gate, exc.code, exc.detail)
            return False, common.PreflightNoStartMessage(str(exc))
        payload['timestamp'] = int(time.time() * 1000)
        def before_publish():
            nonlocal publish_entered, published_at
            if publish_entered:
                raise common.DispatchOutcomeUncertain('S1 publish may already have occurred; never retry automatically')
            fresh = _fresh(printer)
            verify_mapping(filaments, fresh['slots'], mapping)
            # Material/colour must still describe the payload we are about to send.
            for n in mapping:
                if fresh['slots'][n] != ace['slots'][n]:
                    raise S1Refusal('s1_slot_changed', f'Slot {n + 1} changed during dispatch')
            if not common._prepare_post_upload_gate(gate) or not common._post_upload_gate(gate):
                raise S1Refusal(getattr(gate.refusal, 'code', 'gate_rejected'), common._gate_refusal_message(gate))
            from modules.printers.maintenance_gate import check_command
            check_command()
            facts = dict(md5=file['md5'], filesize=file['size'], staged_md5=staged['md5'], staged_size=staged['size'],
                device_copy_differs=file['md5'] != staged['md5'],
                ams_box_mapping=payload['data']['ams_settings']['ams_box_mapping'],
                task_settings=payload['data']['task_settings'], auto_refill=fresh['auto_refill'],
                warnings=['ACE auto_refill is on; it may select another same-material slot'] if fresh['auto_refill'] == 1 else [])
            # Attempt token, not job display name, binds the evidence. Durable even
            # for synchronous dispatch callers without a background progress row.
            from core.durable_config import insert
            with authority.engine.begin() as conn:
                if not insert(conn, 's1_start:' + authority.token, facts):
                    raise S1Refusal('s1_attempt_already_recorded', 'This start attempt already has publish evidence; reconcile before retrying')
            published_at = time.monotonic()
            publish_entered = True
        common._ws(job_id, 'starting', 'File verified; preparing S1 start')
        result = adapter.send_start(payload, before_publish=before_publish)
        if result.get('rejected'):
            raise S1Refusal('s1_start_rejected', result['message'])
        if not result['published']:
            if result.get('uncertain'):
                raise common.DispatchOutcomeUncertain('S1 publish delivery uncertain; never retry automatically')
            raise S1Refusal('s1_send_failed', result['message'])
        if published_at is None or not _confirm_started(printer, remote, deadline=published_at + START_CONFIRM_SECONDS):
            record_refusal(gate, 's1_start_unknown', "The printer passed its start check, but printing and nozzle heat were not observed within 90 seconds. This is a new start failure; tell support. Do not retry automatically. Check that no print started, restart the printer's start service, then try again.")
            raise common.DispatchOutcomeUncertain('S1 start UNKNOWN; never retry automatically')
        return True, 'S1 start confirmed; ' + result['message'] + ('; ACE auto_refill is on' if ace['auto_refill'] == 1 else '')
    except S1Refusal as exc:
        record_refusal(gate, exc.code, exc.detail)
        if publish_entered:
            raise common.DispatchOutcomeUncertain(str(exc)) from exc
        return False, common.NoStartMessage(str(exc))


START_CONFIRM_SECONDS = 90.0


def _confirm_started(printer, remote, *, deadline=None):
    """A QoS0 publish/report is not a start. Correlate fresh physical activity."""
    deadline = time.monotonic() + START_CONFIRM_SECONDS if deadline is None else deadline
    while True:
        try:
            status = printer.get_status()
            # The generic Moonraker parser deliberately labels gkapi-only states
            # unknown; inspect the same fresh response for the S1 state.
            stats = (getattr(status, 'raw_data', None) or {}).get('print_stats') or {}
            state = stats.get('state', status.raw_print_state)
            if (str(state).lower() in ('printing', 'paused', 'heating', 'leveling', 'onpause')
                    and status.nozzle_target > 0
                    and (status.filename or '').lstrip('/') == remote.lstrip('/')
                    and time.monotonic() <= deadline):
                return True
        except Exception:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(1.0, remaining))
