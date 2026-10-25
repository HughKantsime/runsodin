"""Bind a generic observed start to one retained admitted attempt, without I/O."""
from datetime import datetime,timezone
import hmac
from sqlalchemy import text
from core.dispatch_identity import build_identity
from core.dispatch_receipts import generic_filename_token,generic_observed_token,power_fingerprint,observed_filename_matches


def _utc(value):
    stamp=value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def generic_observation_reserved(executor,*,printer_id,observed_filename):
    """Retained authority and reserved namespace never enter fuzzy fallback."""
    from core.dispatch_observation_executor import TransactionView
    conn=TransactionView(executor)
    claim=conn.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE printer_id=:pid'),{'pid':printer_id}).first()
    return claim is not None or (isinstance(observed_filename,str) and 'odin-dispatch-' in observed_filename.lower())


def prepare_generic_observation(executor,*,printer_id,observed_filename):
    """Serialize retained attempts before inserting, and resume only their bound row."""
    from core.dispatch_observation_executor import TransactionView
    from core.order_production import prepare_job_write,ProductionLinkageChanged
    from modules.printers.dispatch_admission import _lock
    conn=TransactionView(executor)
    peek=conn.execute(text('SELECT job_id,token FROM printer_dispatch_claims WHERE printer_id=:pid'),{'pid':printer_id}).first()
    if peek is None:return None
    prepare_job_write(executor,[peek[0]])
    statement=('SELECT job_id,token FROM printer_dispatch_claims WHERE printer_id=:pid FOR UPDATE NOWAIT' if conn.dialect.name=='postgresql' else 'UPDATE printer_dispatch_claims SET token=token WHERE printer_id=:pid RETURNING job_id,token')
    current=conn.execute(text(statement),{'pid':printer_id}).first()
    if current is None or tuple(current)!=tuple(peek):raise ProductionLinkageChanged()
    _lock(conn,'printers',printer_id,'is_active')
    row=conn.execute(text("SELECT p.id,p.filename,p.job_name,r.job_id,p.scheduled_job_id,p.status FROM dispatch_attempt_receipts r JOIN print_jobs p ON p.id=r.observed_print_job_id WHERE r.claim_token=:token AND r.printer_id=:pid AND p.printer_id=:pid"),{'token':current[1],'pid':printer_id}).first()
    if row is None:return None
    protocol=conn.execute(text('SELECT api_type FROM printers WHERE id=:pid'),{'pid':printer_id}).scalar_one()
    name=row[2] if protocol=='bambu' else row[1]
    if name==observed_filename and row[3]==current[0] and row[4]==current[0] and row[5]=='running':
        _lock(conn,'print_jobs',row[0],'status')
        # NOWAIT serializes terminal updates; recheck after the acquired lock.
        fresh=conn.execute(text('SELECT status,scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':row[0]}).first()
        if fresh is not None and fresh[0]=='running' and fresh[1]==current[0]:return row[0]
    return None


def correlate_generic_observation(executor,*,printer_id,print_job_id,key):
    """Caller owns rollback on errors, including contention or linkage races."""
    from core.dispatch_observation_executor import TransactionView
    from core.order_production import prepare_job_write,reconcile_job_write,ProductionLinkageChanged
    conn=TransactionView(executor)
    peek=conn.execute(text('SELECT job_id,token FROM printer_dispatch_claims WHERE printer_id=:pid'),{'pid':printer_id}).first()
    if peek is None:return 'refused'
    parents=prepare_job_write(executor,[peek[0]])
    result=_bind(conn,printer_id=printer_id,print_job_id=print_job_id,key=key,link=True,expected=(peek[0],peek[1]))
    if result=='matched':reconcile_job_write(executor,parents)
    return result


def bind_generic_observation(engine,*,printer_id,print_job_id,key):
    """Idempotent hook; never creates linkage, clears claims or authorizes power."""
    if type(printer_id) is not int or printer_id<=0 or type(print_job_id) is not int or print_job_id<=0:return 'refused'
    try:
        with engine.begin() as conn:
            return _bind(conn,printer_id=printer_id,print_job_id=print_job_id,key=key,link=False)
    except Exception:return 'refused'


def _bind(conn,*,printer_id,print_job_id,key,link,expected=None,education=None):
    statement=('SELECT * FROM printer_dispatch_claims WHERE printer_id=:pid FOR UPDATE NOWAIT'
        if conn.dialect.name=='postgresql' else
        'UPDATE printer_dispatch_claims SET token=token WHERE printer_id=:pid RETURNING *')
    claim=conn.execute(text(statement),{'pid':printer_id}).mappings().first()
    if claim is None:return 'no_claim'
    if expected is not None and (claim['job_id'],claim['token'])!=expected:
        from core.order_production import ProductionLinkageChanged
        raise ProductionLinkageChanged()
    if claim['state'] not in ('adapter_entered','accepted','uncertain'):return 'refused'
    receipt=conn.execute(text('SELECT * FROM dispatch_attempt_receipts WHERE claim_token=:token'),{'token':claim['token']}).mappings().first()
    if (receipt is None or receipt['printer_id']!=printer_id or receipt['job_id']!=claim['job_id']
            or receipt['configuration_key']!=claim['configuration_key']
            or receipt['observed_print_job_id'] not in (None,print_job_id)):
        return 'refused'
    from modules.printers.dispatch_admission import _lock
    _lock(conn,'printers',printer_id,'is_active')
    _lock(conn,'print_jobs',print_job_id,'status')
    observed=conn.execute(text('SELECT printer_id,scheduled_job_id,status,filename,job_name,observed_received_at FROM print_jobs WHERE id=:id'),{'id':print_job_id}).mappings().one()
    if (observed['printer_id']!=printer_id or observed['scheduled_job_id'] not in ((None,claim['job_id']) if link else (claim['job_id'],))
            or observed['status']!='running'):
        return 'refused'
    if education is None:
        if conn.execute(text('SELECT 1 FROM education_submissions WHERE job_id=:id'),{'id':claim['job_id']}).first():return 'refused'
    elif claim['job_id']!=education['job_id'] or not education_observation_authority(conn,education,print_job_id):return 'refused'
    _lock(conn,'jobs',claim['job_id'],'status')
    current_job=conn.execute(text('SELECT printer_id,status FROM jobs WHERE id=:id'),{'id':claim['job_id']}).mappings().one()
    if current_job['printer_id']!=printer_id or current_job['status'] not in ('pending','scheduled','printing'):
        return 'refused'
    if not _matches_prepared_dispatch(conn, printer_id=printer_id, observed=observed,
            claim=claim, receipt=receipt, key=key, education=education):
        return 'refused'
    if link:
        changed=conn.execute(text("UPDATE print_jobs SET scheduled_job_id=:job WHERE id=:id AND printer_id=:pid AND status='running' AND (scheduled_job_id IS NULL OR scheduled_job_id=:job) RETURNING id"),{'job':claim['job_id'],'id':print_job_id,'pid':printer_id}).first()
        if changed is None:raise RuntimeError('Observation linkage changed')
        from core.print_attempts import bind_print_attempt,sqlalchemy_runner
        bind_print_attempt(sqlalchemy_runner(conn),print_job_id,claim['job_id'])
        from modules.jobs.services import transition_job,Expr
        if not transition_job(conn,claim['job_id'],'printing','dispatch',extra_fields={'actual_start':Expr('COALESCE(actual_start,CURRENT_TIMESTAMP)')},where='printer_id=:pid',params={'pid':printer_id}):raise RuntimeError('Scheduled job changed')
    result=conn.execute(text("UPDATE dispatch_attempt_receipts SET observed_print_job_id=:observed WHERE claim_token=:token AND printer_id=:pid AND job_id=:job AND configuration_key=:config AND prepared_power_key=:power AND observed_filename_key=:filename AND (observed_print_job_id IS NULL OR observed_print_job_id=:observed)"),{'observed':print_job_id,'token':claim['token'],'pid':printer_id,'job':claim['job_id'],'config':receipt['configuration_key'],'power':receipt['prepared_power_key'],'filename':receipt['observed_filename_key']})
    if result.rowcount!=1:raise RuntimeError('Observation receipt changed')
    return 'matched'


def _matches_prepared_dispatch(conn, *, printer_id, observed, claim, receipt, key, education=None):
    """Shared exact transport/configuration proof, never standalone authority.

    Caller must first validate and fence the actual claim, receipt, current job,
    printer and any Education transition. This checker neither links an attempt
    nor classifies a candidate as financially owned. Pre-insert callers must
    obtain observed_received_at from the caller's database clock.
    """
    if type(key) is not bytes or len(key)<32:return False
    if receipt['observed_print_job_id'] is None:
        if observed['observed_received_at'] is None:return False
        try:received=_utc(observed['observed_received_at']);created=_utc(receipt['created_at'])
        except (ValueError,TypeError):return False
        clock_sql='SELECT clock_timestamp()' if conn.dialect.name=='postgresql' else 'SELECT CURRENT_TIMESTAMP'
        now=_utc(conn.execute(text(clock_sql)).scalar_one())
        if not 0<=(now-received).total_seconds()<=90 or received<created:return False
    from modules.printers import dispatch
    creds=dispatch._get_printer_info(printer_id,connection=conn)
    identity=build_identity(creds,key)
    if (identity is None or creds.get('is_active') is not True
            or identity.configuration_key!=claim['configuration_key'] or identity.target_key!=claim['physical_key']):
        return False
    transport=observed['job_name'] if creds['api_type']=='bambu' else observed['filename']
    if education is None:
        if generic_observed_token(creds['api_type'],transport)!=claim['token']:return False
    else:
        from core.interfaces.education_policy import parse_education_token
        import hashlib
        token=parse_education_token(transport)
        if token is None or hashlib.sha256(token.encode()).hexdigest()!=education['token_digest']:return False
    broker=None
    plug=creds['plug_configuration']
    if plug.get('plug_type')=='mqtt' and plug.get('plug_auto_on'):
        from modules.notifications.mqtt_republish import get_power_connection_snapshot
        if conn.dialect.name=='postgresql':
            conn.execute(text("SELECT key FROM system_config WHERE key LIKE 'mqtt_republish_%' ORDER BY key FOR UPDATE NOWAIT")).all()
        broker=get_power_connection_snapshot(connection=conn)
        if broker is None:return False
    if (not hmac.compare_digest(receipt['prepared_power_key'],power_fingerprint(key,claim['token'],plug,broker))
            or not observed_filename_matches(key,claim['token'],creds['api_type'],transport,receipt['observed_filename_key'])):
        return False
    return True


def education_observation_authority(conn,context,print_job_id):
    row=conn.execute(text("SELECT 1 FROM education_monitor_claims mc JOIN education_submissions s ON s.id=mc.submission_id AND s.org_id=mc.org_id JOIN jobs j ON j.id=mc.job_id JOIN print_jobs p ON p.id=:observed WHERE mc.claim_id=:claim_id AND mc.org_id=:org_id AND mc.submission_id=:submission_id AND mc.job_id=:job_id AND mc.printer_id=:printer_id AND mc.authority_revision=:authority_revision AND mc.token_digest=:token_digest AND mc.state='running' AND s.job_id=mc.job_id AND s.approved_printer_id=mc.printer_id AND s.status='printing' AND s.lifecycle_revision=mc.authority_revision AND j.charged_to_org_id=mc.org_id AND j.printer_id=mc.printer_id AND j.status='printing' AND p.printer_id=mc.printer_id AND p.scheduled_job_id=mc.job_id AND p.status='running'"),{**context,'observed':print_job_id}).first()
    return row is not None


def bind_education_observation(executor,*,print_job_id,context,key):
    """Called only under the authoritative Education transition/observation locks.

    Semantic refusal retains Education's observation and the universal fence.
    Database and receipt-CAS errors propagate to the enclosing transaction.
    """
    from core.dispatch_observation_executor import TransactionView
    fields=('org_id','submission_id','job_id','printer_id','authority_revision')
    if (not isinstance(context,dict) or any(type(context.get(name)) is not int or context[name]<=0 for name in fields)
            or type(print_job_id) is not int or print_job_id<=0):return 'refused'
    return _bind(TransactionView(executor),printer_id=context['printer_id'],print_job_id=print_job_id,key=key,link=False,education=context)


def publish_observed_start(printer_id,print_job_id,job_name):
    """Delivery failure must not undo a committed observation or request reinsertion."""
    from core.job_timeline import for_attempt
    for_attempt(print_job_id, "started")
    from core.event_bus import get_event_bus
    from core.interfaces.event_bus import Event
    from core import events
    import logging
    try:
        get_event_bus().publish(Event(event_type=events.JOB_STARTED,source_module='printers',data={'printer_id':printer_id,'print_job_id':print_job_id,'job_name':job_name}))
    except Exception:
        logging.getLogger('printers').warning('Committed print start event delivery failed; observation retained')
