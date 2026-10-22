"""Durable dispatch ownership, bounded preparation and exact staged transport."""
import hmac
import os
import time
from core import dispatch_claims
from core.dispatch_identity import build_identity,ownership_refusal
from core.dispatch_target_lock import acquire_target_lock
from core.dispatch_target_guard import unresolved_target
from modules.printers.dispatch_payload_snapshot import stage_payload,hash_regular_payload,PayloadUnavailable
from modules.printers.dispatch_admission import admit

ADMISSION_SECONDS=30.0


class TargetOwnershipLost(RuntimeError):
    pass


def _require_target(handle):
    if not handle.held():
        raise TargetOwnershipLost('Configured target ownership could not be confirmed')


def dispatch_prepared(job,creds,source_path,remote_filename,payload_sha256,education_decision):
    from core.auth import SECRET_KEY
    from core.db import engine
    from modules.printers import dispatch
    key=SECRET_KEY.encode() if isinstance(SECRET_KEY,str) else None
    identity=build_identity(creds,key)
    if identity is None:
        return False,'Dispatch configuration identity could not be verified; no command was sent'
    acquired=acquire_target_lock(engine,identity.target_key)
    if acquired.status!='acquired':
        return False,'A dispatch claim or target ownership conflict/uncertainty blocks dispatch; reconcile before retrying'
    handle=acquired.handle
    result=(False,'Dispatch target ownership requires reconciliation before retrying')
    try:
        _require_target(handle)
        current_job=dispatch._load_job(job['id'])
        current_creds=dispatch._get_printer_info(job['printer_id'])
        current_identity=build_identity(current_creds,key)
        if (current_job is None or current_creds is None or current_identity!=identity
                or current_job['printer_id']!=job['printer_id'] or current_job.get('hold')
                or current_job.get('status') not in ('pending','scheduled')
                or current_creds.get('is_active') is not True
                or ownership_refusal(current_job,current_creds)
                or current_job.get('stored_path')!=source_path):
            result=(False,'Dispatch authority or configuration changed; no command was sent')
        elif dispatch_claims.retire_terminal_locked(engine,printer_id=job['printer_id'],physical_key=identity.target_key,
                configuration_key=identity.configuration_key,owner_org_id=current_creds.get('org_id'),
                target_handle=handle,key=key) not in ('clear','retired'):
            result=(False,'An unresolved dispatch claim or database uncertainty blocks dispatch; check diagnostics and reconcile before retrying')
        elif unresolved_target(engine,job['printer_id'],identity.target_key):
            result=(False,'An unresolved dispatch claim or sender blocks dispatch; reconcile before retrying')
        else:
            _require_target(handle)
            current_education_decision=dispatch._authorize_education_hardware_action(current_job,current_creds)
            if current_education_decision is not None and not current_education_decision[0]:
                result=current_education_decision
            else:
                _require_target(handle)
                result=_dispatch_claimed(current_job,current_creds,source_path,remote_filename,
                    payload_sha256,current_education_decision,handle)
    except Exception:
        result=(False,'Dispatch target ownership requires reconciliation before retrying')
    finally:
        if not handle.close().released:
            result=(False,'Dispatch target cleanup could not be confirmed; reconcile before retrying')
    if result[0] is True:
        # A tiny print may end before adapter return. Only reconcile after our
        # target handle closes; the monitor could not acquire it earlier.
        dispatch_claims.retire_observed_terminal(engine,printer_id=job['printer_id'])
    return result


def _dispatch_claimed(job,creds,source_path,remote_filename,payload_sha256,education_decision,target_handle):
    from core.auth import SECRET_KEY
    from core.db import engine
    from modules.printers import dispatch,smart_plug
    from modules.printers.dispatch_acquisition import bounded_status_is_idle
    key=SECRET_KEY.encode() if isinstance(SECRET_KEY,str) else None
    identity=build_identity(creds,key)
    if identity is None:
        return False,'Dispatch configuration identity could not be verified; no command was sent'
    claim=dispatch_claims.acquire(engine,job['printer_id'],job['id'],identity.target_key,
        identity.configuration_key,payload_sha256,creds.get('org_id'))
    if claim.status!='acquired':
        return False,'An unresolved dispatch claim or database uncertainty blocks dispatch; check diagnostics and reconcile before retrying'
    provider=reservation=None
    adapter_called=False
    ownership_lost=False
    result=(False,'Dispatch preparation could not be confirmed; no print command was sent')
    cleanup=[]
    try:
        with stage_payload(source_path,payload_sha256) as staged:
            options,error=dispatch._validated_print_options(job,creds,staged.path,staged.sha256)
            if error:result=(False,error)
            else:
                if education_decision is not None:
                    reserved,error=dispatch._reserve_education_hardware_action(job,os.path.splitext(remote_filename)[1].lower())
                    if error:result=(False,error)
                    else:provider,reservation=reserved
                if not error:
                    deadline=time.monotonic()+ADMISSION_SECONDS
                    _require_target(target_handle)
                    power=smart_plug.prepare_bounded_dispatch_power(job['printer_id'],deadline)
                    if power.accepted is not True:
                        result=(False,power.error or 'Power preparation was not confirmed; no print command was sent')
                    elif not target_handle.held():
                        raise TargetOwnershipLost()
                    elif not bounded_status_is_idle(creds,deadline):
                        result=(False,'Fresh printer readiness was not confirmed; no print command was sent')
                    elif time.monotonic()>=deadline:
                        result=(False,'Dispatch preparation deadline elapsed; no print command was sent')
                    elif not hmac.compare_digest(hash_regular_payload(source_path),staged.sha256):
                        result=(False,'Print payload changed during preparation; no print command was sent')
                    else:
                        # Generic observations need an exact attempt identity,
                        # independent of display names and reused source names.
                        # Education keeps its authoritative reservation token.
                        transport_filename=(reservation['remote_filename'] if reservation else
                            f"odin-dispatch-{claim.token}{os.path.splitext(staged.path)[1].lower()}")
                        _require_target(target_handle)
                        admission=admit(engine,token=claim.token,job=job,creds=creds,key=key,
                            payload_sha256=staged.sha256,transport_filename=transport_filename,power_accepted=power.accepted,broker_connection=power.connection,
                            education_provider=provider,education_reservation=reservation)
                        if not admission.admitted:result=(False,admission.reason)
                        elif time.monotonic()>=deadline:
                            result=(False,'Dispatch admission completed after the preparation deadline; reconciliation is required before retrying')
                        else:
                            _require_target(target_handle)
                            before_start = None
                            if options.get('ams_mapping') is not None:
                                from modules.printers.dispatch_ams_gate import create_ams_start_gate
                                before_start = create_ams_start_gate(engine,
                                    admission_inputs=dict(token=claim.token,job=job,creds=creds,key=key,
                                        payload_sha256=staged.sha256,transport_filename=transport_filename,
                                        power_accepted=power.accepted,broker_connection=power.connection,
                                        education_provider=provider,education_reservation=reservation),
                                    staged_path=staged.path,mapping=options['ams_mapping'],target_handle=target_handle)
                            adapter_called=True
                            result=dispatch._run_admitted_dispatch(job,creds,staged.path,
                                transport_filename,options,provider,reservation,
                                **({"before_start":before_start} if before_start is not None else {}))
                            _require_target(target_handle)
                            accepted=type(result[0]) is bool and result[0]
                            recorded=dispatch_claims.record_outcome(engine,claim.token,'accepted' if accepted else 'uncertain')
                            if not recorded:
                                result=(False,'Printer command outcome needs database reconciliation before retrying')
                            elif not accepted:
                                result=(False,result[1]+'; Dispatch claim requires explicit reconciliation before retrying')
    except TargetOwnershipLost:
        ownership_lost=True
        if adapter_called:dispatch_claims.record_outcome(engine,claim.token,'uncertain')
        result=(False,'Configured target ownership changed; explicit reconciliation is required before retrying')
    except PayloadUnavailable as exc:
        result=(False,str(exc))
    except Exception:
        if adapter_called:
            dispatch_claims.record_outcome(engine,claim.token,'uncertain')
            result=(False,'Printer command outcome is unknown; explicit reconciliation is required before retrying')
        else:
            result=(False,'Dispatch preparation could not be confirmed; no print command was sent')
    finally:
        if not target_handle.held():ownership_lost=True
        if not adapter_called:
            if reservation and not dispatch._cancel_education_reservation(provider,reservation):
                cleanup.append('Education reservation requires explicit reconciliation')
            if ownership_lost or not dispatch_claims.release_preparing(engine,claim.token):
                cleanup.append('Dispatch claim requires explicit reconciliation')
    if cleanup:
        return False,result[1]+'; '+'; '.join(cleanup)
    return result
