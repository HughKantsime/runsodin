"""Explicit S1 upload/start inside the existing claim and sender boundaries."""
import os
import time
import uuid
from modules.printers.kobra_s1 import S1Refusal, build_payload, read_filaments, mapping_vector, verify_mapping
from modules.printers.dispatch_ams_gate import record_refusal


def options(job, path):
    filaments = read_filaments(path)
    return dict(ams_mapping=mapping_vector(job.get('ams_mapping'), filaments), s1_filaments=filaments)


def create_start_gate(engine, *, admission_inputs, target_handle, **unused):
    from modules.printers.dispatch_admission import recheck_admitted
    def gate():
        if not target_handle.held():
            return record_refusal(gate, 's1_target_changed', 'Configured target ownership changed')
        result = recheck_admitted(engine, **admission_inputs)
        if not result.admitted:
            code, _, detail = result.reason.partition(': ')
            return record_refusal(gate, code, detail or result.reason)
        return True
    gate.refusal = None
    gate.clean_no_start = False
    gate.engine = engine
    gate.job = admission_inputs['job']
    gate.token = admission_inputs['token']
    return gate


def _fresh(printer):
    status = printer.get_status()
    if status.internal_state != 'IDLE' or status.raw_print_state not in ('standby', 'complete', 'cancelled'):
        raise S1Refusal('s1_printer_not_ready', 'S1 must be idle and ready')
    if status.raw_data.get('webhooks', {}).get('state') != 'ready':
        raise S1Refusal('s1_printer_not_ready', 'Klipper is not ready')
    if status.ace_status is None:
        raise S1Refusal('s1_ace_unknown', 'ACE state is unavailable')
    return status.ace_status


def dispatch(job_id, path, remote, creds, print_options, gate):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    from modules.printers.adapters.anycubic_lan import AnycubicLANPrinter
    from modules.printers import dispatch as common
    if not callable(gate) or not hasattr(gate, 'engine'):
        return False, 's1_gate_required: S1 dispatch requires admitted authority'
    printer = MoonrakerPrinter(creds['ip'], creds['port'], creds.get('api_key', ''),
                               model=creds.get('model'), machine_type=creds.get('machine_type'))
    mapping, filaments = print_options['ams_mapping'], print_options['s1_filaments']
    try:
        ace = _fresh(printer)
        verify_mapping(filaments, ace['slots'], mapping)
        if not gate():
            return False, common._gate_refusal_message(gate)
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
        if file['size'] < os.path.getsize(path):
            raise S1Refusal('s1_device_file_incomplete', 'Device copy is smaller than the uploaded file')
        if file['filaments'] != filaments:
            raise S1Refusal('s1_file_changed', 'Device filament header differs from the admitted payload')
        payload = build_payload(file, ace['slots'], mapping, int(time.time() * 1000), str(uuid.uuid4()))
        def before_publish():
            fresh = _fresh(printer)
            verify_mapping(filaments, fresh['slots'], mapping)
            # Material/colour must still describe the payload we are about to send.
            for n in mapping:
                if fresh['slots'][n] != ace['slots'][n]:
                    raise S1Refusal('s1_slot_changed', f'Slot {n + 1} changed during dispatch')
            if not gate():
                raise S1Refusal(gate.refusal.code, gate.refusal.detail)
            from modules.printers.maintenance_gate import check_command
            check_command()
            facts = dict(md5=file['md5'], filesize=file['size'],
                ams_box_mapping=payload['data']['ams_settings']['ams_box_mapping'],
                task_settings=payload['data']['task_settings'], auto_refill=fresh['auto_refill'],
                warnings=['ACE auto_refill is on; it may select another same-material slot'] if fresh['auto_refill'] == 1 else [])
            # Attempt token, not job display name, binds the evidence. Durable even
            # for synchronous dispatch callers without a background progress row.
            from core.durable_config import insert
            with gate.engine.begin() as conn:
                if not insert(conn, 's1_start:' + gate.token, facts):
                    raise S1Refusal('s1_attempt_already_recorded', 'This start attempt already has publish evidence; reconcile before retrying')
        common._ws(job_id, 'starting', 'File verified; preparing S1 start')
        result = AnycubicLANPrinter(creds['ip']).send_start(payload, before_publish=before_publish)
        if result.get('rejected'):
            raise S1Refusal('s1_start_rejected', result['message'])
        if not result['published']:
            if result.get('uncertain'):
                raise common.DispatchOutcomeUncertain('S1 publish delivery uncertain; never retry automatically')
            raise S1Refusal('s1_send_failed', result['message'])
        return True, result['message'] + ('; ACE auto_refill is on' if ace['auto_refill'] == 1 else '')
    except S1Refusal as exc:
        record_refusal(gate, exc.code, exc.detail)
        return False, str(exc)
