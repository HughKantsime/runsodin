"""Private generic observed-start preparation; caller owns the outer transaction.

No monitor activation, valuation, snapshot publication or external I/O occurs here.
"""
from dataclasses import dataclass,field
from sqlalchemy import text
from core.economic_source_creation import SourceCreationError,_create_attempt,_validate_existing_source
from core.dispatch_observation_executor import TransactionView
from core.order_production import prepare_parent_graph,recheck_parent_graph,reconcile_job_write,ProductionLinkageChanged
from core.dispatch_observations import _matches_prepared_dispatch,_bind

_SEAL=object()

@dataclass(frozen=True,eq=False)
class _StartPlan:
    branch:str
    job_id:int|None
    resume_id:int|None
    reason:str|None
    reserved:bool
    _seal:object=field(repr=False)


def _record(executor,scope,plan):
    scope.require_fenced(executor,education=True)
    record=scope._start_plan_records.get(id(plan))
    if type(plan) is not _StartPlan or plan._seal is not _SEAL or record is None or record['plan'] is not plan or record['executor'] is not executor or record['finalized']:
        raise SourceCreationError('Start plan is no longer current')
    return record


def _lock(view,table,identifier):
    from modules.printers.dispatch_admission import _lock as lock
    lock(view,table,identifier,'status' if table in ('jobs','print_jobs') else ('token' if table=='printer_dispatch_claims' else 'is_active'))


def _claim(view,printer):
    return view.execute(text('SELECT * FROM printer_dispatch_claims WHERE printer_id=:id'),{'id':printer}).mappings().first()


def _receipt(view,token):
    return view.execute(text('SELECT * FROM dispatch_attempt_receipts WHERE claim_token=:token'),{'token':token}).mappings().first()


def _proof(view,record,observed):
    claim=_claim(view,record['printer'])
    receipt=_receipt(view,claim['token']) if claim else None
    if claim is None or receipt is None or dict(claim)!=record['claim'] or dict(receipt)!=record['receipt']:
        return False
    job=view.execute(text('SELECT printer_id,status FROM jobs WHERE id=:id'),{'id':claim['job_id']}).first()
    if job is None or job[0]!=record['printer'] or job[1] not in ('pending','scheduled','printing'):
        return False
    education=record.get('education')
    if education is None:
        if view.execute(text('SELECT 1 FROM education_submissions WHERE job_id=:id'),{'id':claim['job_id']}).first():return False
    elif (not record['education_provider'].start_identity_matches(education, *record['incoming_identities'], observed.get('filename'), observed.get('job_name'))
            or claim['job_id']!=education['job_id'] or not record['education_provider'].recheck_start_authority(view.executor,education)):
        return False
    return _matches_prepared_dispatch(view,printer_id=record['printer'],observed=observed,claim=claim,receipt=receipt,key=record['key'],education=education)


def prepare_start(executor,scope,*,printer_id,observed_filename,protocol,observation_fields,capture_required=False):
    """Prepare before any attempt INSERT; absence is classified under table fences."""
    scope.require_fenced(executor,education=True)
    if type(capture_required) is not bool:raise SourceCreationError('Invalid capture mode')
    if type(printer_id) is not int or printer_id<=0 or protocol not in ('bambu','moonraker','prusalink','elegoo','anycubic_lan') or not isinstance(observed_filename,str) or not observed_filename or not isinstance(observation_fields,dict):
        raise SourceCreationError('Invalid start observation')
    fields={name:observation_fields.get(name) for name in ('filename','job_name')}
    transport=fields['job_name'] if protocol=='bambu' else fields['filename']
    if transport!=observed_filename:raise SourceCreationError('Observation transport differs')
    view=TransactionView(executor)
    received=view.execute(text('SELECT clock_timestamp()' if view.dialect.name=='postgresql' else 'SELECT CURRENT_TIMESTAMP')).scalar_one()
    observed={**fields,'printer_id':printer_id,'status':'running','scheduled_job_id':None,'observed_received_at':received}
    claim=_claim(view,printer_id)
    from core.registry import registry
    from core.interfaces.education_policy import parse_education_token
    education_provider=registry.get_provider('EducationPolicyProvider')
    education=None
    education_token=parse_education_token(observed_filename)
    if education_token is not None:
        discover=getattr(education_provider,'discover_start_authority',None)
        if not callable(discover):raise SourceCreationError('Education preinsert authority unavailable')
        education=discover(executor,printer_id=printer_id,observed_filename=observed_filename)
    if claim is None and education is None:_lock(view,'printers',printer_id)
    reserved=claim is not None or education_token is not None or 'odin-dispatch-' in observed_filename.lower()
    record={'executor':executor,'printer':printer_id,'protocol':protocol,'observed':observed,'key':observation_fields.get('dispatch_key'),'graph':None,'claim':None,'receipt':None,'membership_claim':dict(claim) if claim else None,'membership_receipt':dict(_receipt(view,claim['token'])) if claim and _receipt(view,claim['token']) else None,'created':None,'finalized':False,'education':education,'education_provider':education_provider,'capture_required':capture_required,'capture_completed':False,'incoming_identities':(observed_filename,fields['filename'],fields['job_name'])}
    branch='fresh_unresolved';job_id=resume_id=None;reason='no_authority'
    if claim is not None or education is not None:
        candidates=([claim['job_id']] if claim else [])+([education['job_id']] if education else [])
        graph=prepare_parent_graph(executor,candidates);record['graph']=graph
        if education is not None:
            education_provider.lock_start_authority(executor,education)
        if claim is not None and (education is None or claim['job_id']!=education['job_id']):_lock(view,'jobs',claim['job_id'])
        recheck_parent_graph(executor,graph)
        # Exact claim lock follows parents/jobs and precedes printer/attempt.
        query='SELECT * FROM printer_dispatch_claims WHERE printer_id=:id FOR UPDATE NOWAIT' if view.dialect.name=='postgresql' else 'UPDATE printer_dispatch_claims SET token=token WHERE printer_id=:id RETURNING *'
        current=view.execute(text(query),{'id':printer_id}).mappings().first()
        if (dict(current) if current else None)!=(dict(claim) if claim else None):raise ProductionLinkageChanged()
        _lock(view,'printers',printer_id)
        receipt=_receipt(view,claim['token']) if claim else None
        if (claim is not None and claim['state'] in ('adapter_entered','accepted','uncertain') and receipt is not None and receipt['printer_id']==printer_id and receipt['job_id']==claim['job_id'] and receipt['configuration_key']==claim['configuration_key']):
            record['claim']=dict(claim);record['receipt']=dict(receipt)
            actual_protocol=view.execute(text('SELECT api_type FROM printers WHERE id=:id'),{'id':printer_id}).scalar_one()
            if actual_protocol==protocol:
                existing=receipt['observed_print_job_id']
                if existing is not None:
                    _lock(view,'print_jobs',existing)
                    row=view.execute(text('SELECT printer_id,scheduled_job_id,status,filename,job_name,observed_received_at,economic_birth_nonce FROM print_jobs WHERE id=:id'),{'id':existing}).mappings().first()
                    if row is not None and (row['job_name'] if protocol=='bambu' else row['filename'])==observed_filename and row['printer_id']==printer_id and row['scheduled_job_id']==claim['job_id'] and row['status']=='running' and _proof(view,record,row):
                        if row['economic_birth_nonce'] is not None:_validate_existing_source(executor,'attempt',existing)
                        branch='legacy_resume' if row['economic_birth_nonce'] is None else 'resume';job_id=claim['job_id'];resume_id=existing;reason=None;record['observed']=dict(row)
                elif _proof(view,record,observed):
                    branch='fresh_authorized';job_id=claim['job_id'];reason=None
        if branch=='fresh_unresolved':reason='reserved_authority_refused'
    plan=_StartPlan(branch,job_id,resume_id,reason,reserved,_SEAL)
    record['plan']=plan;scope._start_plan_records[id(plan)]=record
    return plan


def create_start(executor,scope,plan,physical_values):
    """Module owns factory call/result; callers cannot substitute CreatedSource."""
    record=_record(executor,scope,plan)
    if plan.branch not in ('fresh_authorized','fresh_unresolved') or record['created'] is not None:
        raise SourceCreationError('Start plan cannot create another attempt')
    if not isinstance(physical_values,dict) or set(physical_values)&{'scheduled_job_id','economic_uid','economic_birth_nonce','id','observed_received_at','printer_id','filename','job_name','status'}:
        raise SourceCreationError('Physical start fields override prepared authority')
    view=TransactionView(executor)
    if record['graph'] is not None:recheck_parent_graph(executor,record['graph'])
    if plan.branch=='fresh_authorized' and not _proof(view,record,record['observed']):
        raise SourceCreationError('Prepared start authority changed')
    if plan.branch=='fresh_unresolved':
        # Reprepare under the same scope; any branch/membership drift is refusal,
        # not a hidden promotion of this permanently unresolved plan.
        current=_claim(view,record['printer'])
        receipt=_receipt(view,current['token']) if current else None
        if (dict(current) if current else None)!=record['membership_claim'] or (dict(receipt) if receipt else None)!=record['membership_receipt']:
            raise SourceCreationError('Prepared unresolved membership changed')
    values={**physical_values,**{name:record['observed'][name] for name in ('filename','job_name','observed_received_at')},'printer_id':record['printer'],'status':'running','scheduled_job_id':plan.job_id}
    created=_create_attempt(executor,values)
    record['created']=created
    scope.checkpoint(executor)
    return created.id


def finalize_start(executor,scope,plan):
    """Consume only own exact inserted row; any failure requires outer rollback."""
    record=_record(executor,scope,plan);view=TransactionView(executor)
    if plan.branch not in ('resume','legacy_resume') and record['capture_required'] and not record['capture_completed']:
        raise SourceCreationError('Fresh start capture must complete before finalization')
    if record['graph'] is not None:recheck_parent_graph(executor,record['graph'])
    if plan.branch in ('resume','legacy_resume'):
        row=view.execute(text('SELECT printer_id,scheduled_job_id,status,filename,job_name,observed_received_at,economic_birth_nonce FROM print_jobs WHERE id=:id'),{'id':plan.resume_id}).mappings().first()
        if row is None or dict(row)!=record['observed'] or not _proof(view,record,row) or row['scheduled_job_id']!=plan.job_id or row['status']!='running':raise SourceCreationError('Resumed start authority changed')
        if plan.branch=='resume':_validate_existing_source(executor,'attempt',plan.resume_id)
        elif row['economic_birth_nonce'] is not None:raise SourceCreationError('Legacy resume source changed')
        identifier=plan.resume_id
    else:
        created=record['created']
        if created is None:raise SourceCreationError('Start plan has no owned fresh result')
        row=view.execute(text('SELECT economic_birth_nonce,economic_uid,printer_id,scheduled_job_id,status,filename,job_name,observed_received_at FROM print_jobs WHERE id=:id'),{'id':created.id}).mappings().first()
        if row is None or (row['economic_birth_nonce'],row['economic_uid'],row['printer_id'],row['scheduled_job_id'],row['status'])!=(created.birth_nonce,created.identity_uid,record['printer'],plan.job_id,'running') or any(row[name]!=record['observed'][name] for name in ('filename','job_name','observed_received_at')):
            raise SourceCreationError('Owned fresh start result changed')
        _validate_existing_source(executor,'attempt',created.id)
        if plan.branch=='fresh_authorized':
            if not _proof(view,record,row):raise SourceCreationError('Prepared start authority changed')
            if record.get('education') is not None:
                result=record['education_provider'].finalize_start_authority(executor,scope=scope,plan=plan,print_job_id=created.id,context=record['education'])
                if not result.get('authorized') or result.get('dispatch_receipt_binding')!='matched':raise SourceCreationError('Prepared Education start binding refused')
            else:
                result=_bind(view,printer_id=record['printer'],print_job_id=created.id,key=record['key'],link=True,expected=(plan.job_id,record['claim']['token']))
                if result!='matched':raise SourceCreationError('Prepared start binding refused')
            reconcile_job_write(executor,record['graph'][0])
        else:
            current=_claim(view,record['printer']);receipt=_receipt(view,current['token']) if current else None
            if (dict(current) if current else None)!=record['membership_claim'] or (dict(receipt) if receipt else None)!=record['membership_receipt']:
                raise SourceCreationError('Prepared unresolved membership changed')
        identifier=created.id
    scope.checkpoint(executor);record['finalized']=True;record['key']=None
    return identifier


@dataclass(frozen=True)
class FreshCaptureContext:
    id:int
    birth_nonce:str
    identity_uid:str|None
    job_id:int|None
    parent_graph:tuple|None


def _owned_fresh_capture(executor,scope,plan,attempt_id):
    record=_record(executor,scope,plan)
    created=record['created']
    if plan.branch in ('resume','legacy_resume') or created is None or type(attempt_id) is not int or attempt_id!=created.id or record['capture_completed']:
        raise SourceCreationError('Fresh capture capability unavailable')
    if record['graph'] is not None:recheck_parent_graph(executor,record['graph'])
    row=TransactionView(executor).execute(text('SELECT economic_birth_nonce,economic_uid,scheduled_job_id,printer_id,status,spool_snapshot FROM print_jobs WHERE id=:id'),{'id':attempt_id}).first()
    if row is None or tuple(row[:5])!=(created.birth_nonce,created.identity_uid,plan.job_id,record['printer'],'running'):
        raise SourceCreationError('Fresh capture source changed')
    return record,row


def _fresh_capture_context(executor,scope,plan,attempt_id):
    record,row=_owned_fresh_capture(executor,scope,plan,attempt_id)
    if row[5] is not None:raise SourceCreationError('Fresh start snapshot already exists')
    created=record['created']
    return FreshCaptureContext(created.id,created.birth_nonce,created.identity_uid,plan.job_id,record['graph'])


def _complete_fresh_capture(executor,scope,plan,attempt_id):
    record,row=_owned_fresh_capture(executor,scope,plan,attempt_id)
    if row[5] is None:raise SourceCreationError('Fresh start snapshot CAS has not completed')
    record['capture_completed']=True
