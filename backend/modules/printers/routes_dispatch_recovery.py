"""Scoped retained-dispatch recovery. All device evidence is passive DB telemetry."""
from typing import Literal
from fastapi import APIRouter, Depends, Path, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role, require_action
from core.dispatch_recovery import capture, authorized, mutate, telemetry, GUIDANCE

class SafeRecoveryRoute(APIRoute):
    """Do not echo invalid inputs or cache private evidence/expectation handles."""
    def get_route_handler(self):
        original = super().get_route_handler()
        async def safe(request):
            try:
                response = await original(request)
                response.headers['Cache-Control'] = 'private, no-store'
                return response
            except RequestValidationError:
                raise HTTPException(422, {'code':'invalid_request',
                    'message':'Invalid recovery request. Check the required fields.',
                    'refresh_required':True},headers={'Cache-Control':'private, no-store'}) from None
        return safe


router = APIRouter(route_class=SafeRecoveryRoute)

class RecoveryExpectation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    attempt_handle: str = Field(pattern=r'^dr1\.[0-9a-f]{64}$')
    snapshot_handle: str = Field(pattern=r'^ds1\.[0-9a-f]{64}$')

class HoldRequest(RecoveryExpectation):
    reason_category: Literal['investigation','suspected_duplicate','ownership_review']

class RetainRequest(RecoveryExpectation):
    reason_category: Literal['investigation','sender_unverified','transport_queue_unverified','status_unavailable','education_authority_unresolved','physical_check_inconclusive']
    education_action: Literal['none','request_unbound_cancellation']

class Attestation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal[1]
    printer_checked: Literal[True]
    sender_stopped_and_checked: Literal[True]
    transport_and_device_queues_checked: Literal[True]
    no_pending_dispatch_can_arrive: Literal[True]

    @field_validator('version', 'printer_checked', 'sender_stopped_and_checked', 'transport_and_device_queues_checked', 'no_pending_dispatch_can_arrive', mode='before')
    @classmethod
    def exact_values(cls, value, info):
        if info.field_name == 'version':
            if type(value) is not int or value != 1: raise ValueError('Version must be 1')
        elif value is not True:
            raise ValueError('Every attestation must be true')
        return value


class ReleaseRequest(RecoveryExpectation):
    reason_category: Literal['verified_no_pending_dispatch','verified_terminal_reconciled']
    attestation: Attestation


def configured_identity(printer_id, connection):
    from modules.printers.dispatch import _get_printer_info
    from core.auth import SECRET_KEY
    from core.dispatch_identity import build_identity
    return build_identity(_get_printer_info(printer_id,connection=connection),SECRET_KEY.encode())


@router.get('/printers/{printer_id}/dispatch-recovery',tags=['Printers'])
def inspect_dispatch_recovery(printer_id:int=Path(gt=0), current_user:dict=Depends(require_role('viewer',scope='read:printers')), db:Session=Depends(get_db)):
    evidence = capture(db,printer_id,current_user,lambda conn,pid=printer_id:configured_identity(pid,conn))
    if evidence is None:
        return {'state':'no_retained_claim','release_available':False,'actions':{'hold':False,'reconcile_retain':False,'release':False},'blockers':[],'guidance':'Missing retained evidence is not permission to retry. '+GUIDANCE}
    claim,job = evidence['claim'],evidence['job']
    visible = evidence['visible']
    from core.rbac import is_scoped_token, token_scope_granted
    from core.rbac_policy import action_roles
    can_write = (current_user.get('role')=='admin'
                 and (current_user.get('group_id') is None or 'admin' in action_roles(db,'printers.dispatch_recovery'))
                 and (not is_scoped_token(current_user) or token_scope_granted(current_user.get('_token_scopes',[]),'write:printers')))
    if can_write:
        try: authorized(db,evidence,current_user)
        except Exception: can_write=False
    receipt = next((r for r in evidence['receipts'] if claim and r['claim_token']==claim['token']),None)
    observed = next((o for o in evidence['observations'] if receipt and o['id']==receipt['observed_print_job_id'] and o['scheduled_job_id']==claim['job_id']),None)
    edu_visible = visible and bool(evidence['submissions'])
    if edu_visible:
        from core.registry import registry
        provider = registry.get_provider('EducationPolicyProvider')
        visibility = getattr(provider,'recovery_visibility',None)
        edu_visible = bool(callable(visibility) and visibility(db,evidence=evidence,principal=current_user))
    blockers = []
    if not visible: blockers.append('authority_unavailable')
    if not job or not job['hold']: blockers.append('hold_required')
    if evidence['reservations']:
        blockers.append('education_authority_unresolved')
    elif evidence['submissions']:
        from core.registry import registry
        provider = registry.get_provider('EducationPolicyProvider')
        verifier = getattr(provider,'recovery_terminal_resolved',None)
        if not callable(verifier) or not verifier(db,evidence=evidence): blockers.append('education_authority_unresolved')
    if job and str(job['status']).lower() in ('printing','running','paused'): blockers.append('active_observation')
    failure = telemetry(db,evidence)
    if failure: blockers.append(failure)
    # GET is a diagnostic, not lock acquisition or permission to send. A release
    # always repeats every prerequisite under target and authority exclusion.
    from core.dispatch_target_guard import unresolved_target
    if claim and unresolved_target(db.get_bind(),printer_id,claim['physical_key'],connection=db.connection()):
        blockers.append('sender_quiescence_unverified')
    if can_write and claim:
        identity = configured_identity(printer_id,db.connection())
        if identity is None or identity.target_key!=claim['physical_key'] or identity.configuration_key!=claim['configuration_key']:
            blockers.append('configuration_changed')
    if can_write and claim:
        from core.dispatch_recovery import release_evidence_checks
        from fastapi import HTTPException
        try:
            release_evidence_checks(db,evidence)
        except HTTPException as error:
            code = error.detail['code']
            if code not in blockers: blockers.append(code)
    available = bool(can_write and claim and not blockers)
    result = {'state':claim['state'] if claim else 'education_reservation_retained',
        'job_id':job['id'] if visible else None,
        'job':{'status':job['status'],'held':bool(job['hold'])} if visible else None,
        'receipt_present':receipt is not None,
        'observed_print':{'id':observed['id'],'status':observed['status']} if visible and observed else None,
        'education':{'submission_id':evidence['submissions'][0]['id'],'status':evidence['submissions'][0]['status'],
                     'reservation_state':evidence['reservations'][0]['state'] if evidence['reservations'] else None} if edu_visible else None,
        'release_available':available,'actions':{'hold':can_write,'reconcile_retain':can_write,'release':available},
        'blockers':blockers,'guidance':GUIDANCE}
    if visible:
        result.update(attempt_handle=evidence['attempt'],snapshot_handle=evidence['snapshot'])
    return result


@router.post('/printers/{printer_id}/dispatch-recovery/hold',tags=['Printers'],dependencies=[Depends(require_action('printers.dispatch_recovery'))])
def hold_dispatch_recovery(body:HoldRequest,printer_id:int=Path(gt=0),current_user:dict=Depends(require_role('admin',scope='write:printers')),db:Session=Depends(get_db)):
    return mutate(db,printer_id,current_user,body,'hold',lambda conn,pid=printer_id:configured_identity(pid,conn))


@router.post('/printers/{printer_id}/dispatch-recovery/reconcile-retain',tags=['Printers'],dependencies=[Depends(require_action('printers.dispatch_recovery'))])
def retain_dispatch_recovery(body:RetainRequest,printer_id:int=Path(gt=0),current_user:dict=Depends(require_role('admin',scope='write:printers')),db:Session=Depends(get_db)):
    return mutate(db,printer_id,current_user,body,'reconcile-retain',lambda conn,pid=printer_id:configured_identity(pid,conn))


@router.post('/printers/{printer_id}/dispatch-recovery/release',tags=['Printers'],dependencies=[Depends(require_action('printers.dispatch_recovery'))])
def release_dispatch_recovery(body:ReleaseRequest,printer_id:int=Path(gt=0),current_user:dict=Depends(require_role('admin',scope='write:printers')),db:Session=Depends(get_db)):
    return mutate(db,printer_id,current_user,body,'release',lambda conn,pid=printer_id:configured_identity(pid,conn))
