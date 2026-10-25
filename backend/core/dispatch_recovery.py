"""Caller-owned recovery transactions. No adapter, printer command, or retry.

Target exclusion proves participating ODIN sends are absent, not that external
senders or device queues are empty. The operator attests to that unseen scope.
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone
from importlib import import_module
from fastapi import HTTPException
from sqlalchemy import text, delete
from core.rbac import check_org_access
from core.registry import registry
from core.dispatch_claim_schema import PrinterDispatchClaim
from core.dispatch_target_guard import unresolved_target

IDENTITY = ('printer_id','job_id','owner_org_id','physical_key','configuration_key','payload_sha256','token')
GUIDANCE = ('Hold stops future admission but cannot stop an admitted command. Idle status, silence and elapsed time do not prove safety. '
            'ODIN cannot see non-ODIN senders such as Bambu Studio or a print queued on the device; the operator attestation covers these.')


def refuse(code, status=409):
    raise HTTPException(status, {'code':code,'message': 'Dispatch recovery requires refreshed evidence or authorized investigation.', 'refresh_required':True})


def rows(db, sql, values):
    return [dict(r) for r in db.execute(text(sql), values).mappings()]


def capture(db, printer_id, actor, identity_loader=None):
    args = {'pid':printer_id}
    printers = rows(db, 'SELECT * FROM printers WHERE id=:pid', args)
    if not printers or printers[0]['org_id'] is None or not check_org_access(actor, printers[0]['org_id']):
        refuse('not_found',404)
    printer = printers[0]
    claims = rows(db,'SELECT * FROM printer_dispatch_claims WHERE printer_id=:pid',args)
    reservations = rows(db,'SELECT * FROM education_monitor_claims WHERE printer_id=:pid ORDER BY claim_id',args)
    claim = claims[0] if claims else None
    if claim and (claim['owner_org_id'] is None or claim['owner_org_id'] != printer['org_id'] or not check_org_access(actor,claim['owner_org_id'])):
        refuse('not_found',404)
    if not claim and not reservations:
        return None
    anchor = claim or reservations[0]
    job_id = anchor['job_id']
    jobs = rows(db,'SELECT * FROM jobs WHERE id=:id',{'id':job_id})
    job = jobs[0] if jobs else None
    visible = bool(job and job['charged_to_org_id'] is not None and check_org_access(actor,job['charged_to_org_id']) and job['printer_id']==printer_id)
    submissions = rows(db,'SELECT * FROM education_submissions WHERE job_id=:id ORDER BY id',{'id':job_id})
    receipts = rows(db,'SELECT * FROM dispatch_attempt_receipts WHERE printer_id=:pid ORDER BY claim_token',args)
    target_ids = [printer_id]
    if claim and identity_loader:
        for candidate in rows(db,'SELECT id FROM printers ORDER BY id',{}):
            if candidate['id']==printer_id: continue
            identity = identity_loader(db.connection(),candidate['id'])
            if identity and identity.target_key==claim['physical_key']: target_ids.append(candidate['id'])
    from sqlalchemy import bindparam
    observation_query = text('SELECT id,printer_id,scheduled_job_id,status,started_at,ended_at FROM print_jobs WHERE printer_id IN :ids ORDER BY id').bindparams(bindparam('ids',expanding=True))
    observations = [dict(r) for r in db.execute(observation_query,{'ids':target_ids}).mappings()]
    job_query = text('SELECT id,printer_id,status,hold FROM jobs WHERE printer_id IN :ids ORDER BY id').bindparams(bindparam('ids',expanding=True))
    target_jobs = [dict(r) for r in db.execute(job_query,{'ids':target_ids}).mappings()]
    from core.auth import SECRET_KEY
    key = SECRET_KEY.encode()
    def mac(purpose, value):
        return purpose + '.' + hmac.new(key, purpose.encode()+b'\0'+json.dumps(value,sort_keys=True,default=str,separators=(',',':')).encode(),hashlib.sha256).hexdigest()
    immutable = {k:claim[k] for k in IDENTITY} if claim else {k:anchor[k] for k in ('claim_id','org_id','submission_id','job_id','printer_id','authority_revision','token_digest')}
    attempt = mac('dr1', immutable)
    # Full configuration and relevant resource rows are MAC inputs only; never
    # serialize them to a response, log, decision, or error.
    configuration_fields = ('api_type','api_host','api_key','bed_x_mm','bed_y_mm','nozzle_diameter','machine_type','model',
                            'org_id','is_active','shared','plug_type','plug_host','plug_entity_id','plug_auth_token','plug_auto_on')
    configured = {k:printer.get(k) for k in configuration_fields}
    configured['slots'] = rows(db,'SELECT slot_number,filament_type FROM filament_slots WHERE printer_id=:pid ORDER BY slot_number',args)
    senders = rows(db,'SELECT * FROM dispatch_sender_registrations WHERE printer_id=:pid OR physical_key=:key ORDER BY claim_token',{'pid':printer_id,'key':claim['physical_key'] if claim else ''})
    power = rows(db,'SELECT * FROM power_target_operations WHERE printer_id=:pid OR target_key=:key ORDER BY operation_nonce',{'pid':printer_id,'key':claim['physical_key'] if claim else ''})
    snapshot = mac('ds1',[attempt,claim,job,receipts,observations,reservations,submissions,configured,senders,power,target_ids,target_jobs])
    return dict(printer=printer,claim=claim,reservations=reservations,job=job,visible=visible,submissions=submissions,receipts=receipts,observations=observations,target_jobs=target_jobs,attempt=attempt,snapshot=snapshot)


def education_authority(db, evidence, actor, lock=False):
    if not evidence['submissions'] and not evidence['reservations']:
        return True
    provider = registry.get_provider('EducationPolicyProvider')
    method = getattr(provider,'recovery_authority',None)
    return bool(callable(method) and method(db, evidence=evidence, principal=actor, lock=lock))


def authorized(db, evidence, actor, lock=False):
    if not evidence['visible']:
        refuse('not_found',404)
    if not education_authority(db,evidence,actor,lock):
        refuse('not_found',404)


def telemetry(db, evidence):
    p = evidence['printer']
    # PostgreSQL CURRENT_TIMESTAMP freezes at transaction start; final freshness
    # must use the database wall clock after authority locking.
    clock = 'SELECT clock_timestamp()' if db.get_bind().dialect.name=='postgresql' else 'SELECT CURRENT_TIMESTAMP'
    now = db.execute(text(clock)).scalar_one()
    def stamp(value):
        value = value if isinstance(value,datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    try:
        age = (stamp(now)-stamp(p['last_seen'])).total_seconds()
        if not 0 <= age <= 10: return 'status_unavailable'
    except (ValueError,TypeError):
        return 'status_unavailable'
    state = str(p.get('gcode_state') or '').upper()
    if state in ('RUNNING','PRINTING','PAUSE','PAUSED','PREPARE','PREPARING'):
        return 'active_observation'
    if state not in ('IDLE','FINISH','FINISHED','COMPLETED','FAILED','CANCELLED','READY','STANDBY'):
        return 'status_unavailable'
    if any(str(j['status']).lower() in ('printing','running','paused') for j in evidence['target_jobs']):
        return 'active_observation'
    if any(str(o['status']).lower() not in ('completed','cancelled','failed') for o in evidence['observations']):
        return 'active_observation'
    return None


def release_checks(db, evidence, identity):
    claim = evidence['claim']
    if not claim:
        refuse('education_authority_unresolved')
    if identity is None or identity.target_key != claim['physical_key'] or identity.configuration_key != claim['configuration_key']:
        refuse('configuration_changed')
    conn = db.connection()
    senders = rows(db,'SELECT state,finished_at,cleanup_evidence_key FROM dispatch_sender_registrations WHERE printer_id=:pid OR physical_key=:key',{'pid':claim['printer_id'],'key':claim['physical_key']})
    if any(s['state'] not in ('active','cleanup_uncertain','quiesced') or
           s['state']=='quiesced' and (not s['finished_at'] or not s['cleanup_evidence_key']) for s in senders):
        refuse('evidence_unavailable')
    if unresolved_target(conn.engine,claim['printer_id'],claim['physical_key'],connection=conn):
        refuse('sender_quiescence_unverified')
    others = db.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE physical_key=:key AND token<>:token'),{'key':claim['physical_key'],'token':claim['token']}).first()
    if others: refuse('target_busy')
    failure = telemetry(db,evidence)
    if failure: refuse(failure)


def release_evidence_checks(db, current):
    """Shared sanitized eligibility checks; grants no target lock or send right."""
    job = current['job']
    claim = current['claim']
    if job['hold'] is not True and not (type(job['hold']) is int and job['hold']==1): refuse('hold_required')
    if current['reservations']: refuse('education_authority_unresolved')
    if current['submissions']:
        provider = registry.get_provider('EducationPolicyProvider')
        verifier = getattr(provider,'recovery_terminal_resolved',None)
        if not callable(verifier) or not verifier(db,evidence=current): refuse('education_authority_unresolved')
    if str(job['status']).lower() in ('printing','running','paused'): refuse('active_observation')
    # A bound receipt requires genuine matching terminal truth. Recovery
    # never manufactures a terminal transition or retires a live record.
    claim = current['claim']
    receipt = next((r for r in current['receipts'] if r['claim_token']==claim['token']),None)
    if receipt and (receipt['configuration_key']!=claim['configuration_key'] or receipt['job_id']!=job['id']):
        refuse('evidence_unavailable')
    if receipt and receipt['observed_print_job_id'] is not None:
        observed = next((o for o in current['observations'] if o['id']==receipt['observed_print_job_id']),None)
        if not observed or observed['scheduled_job_id']!=job['id'] or observed['status']!=job['status'] or not observed['ended_at'] or str(observed['ended_at'])!=str(job['actual_end']):
            refuse('evidence_unavailable')
        if not current['submissions']:
            from core.dispatch_receipts import observed_filename_matches
            from core.auth import SECRET_KEY
            names = rows(db,'SELECT filename,job_name FROM print_jobs WHERE id=:id',{'id':observed['id']})[0]
            protocol = current['printer']['api_type']
            if (not observed['started_at'] or not job['actual_start'] or str(observed['ended_at']) < str(observed['started_at'])
                    or not observed_filename_matches(SECRET_KEY.encode(),claim['token'],protocol,
                        names['job_name'] if protocol=='bambu' else names['filename'],receipt['observed_filename_key'])):
                refuse('evidence_unavailable')


def mutate(db, printer_id, actor, request, action, identity_loader):
    from core.dispatch_target_lock import acquire_target_lock
    from core.order_production import prepare_parent_graph, recheck_parent_graph
    from core.audit import log_audit
    handle = None
    committing = False
    try:
        before = capture(db,printer_id,actor,identity_loader)
        if before is None: refuse('attempt_changed')
        authorized(db,before,actor)
        if not hmac.compare_digest(request.attempt_handle,before['attempt']): refuse('attempt_changed')
        db.rollback()  # target exclusion must never be acquired in a row TX
        if action == 'release':
            claim = before['claim']
            if claim is None: refuse('education_authority_unresolved')
            acquired = acquire_target_lock(db.get_bind(),claim['physical_key'])
            if acquired.status != 'acquired':
                refuse('target_busy' if acquired.status=='conflict' else 'unsupported_locking' if acquired.status=='unsupported' else 'evidence_unavailable')
            handle = acquired.handle
            if not handle.held(): refuse('evidence_unavailable')
            current = capture(db,printer_id,actor,identity_loader)
            if current is None or current['attempt'] != before['attempt']: refuse('attempt_changed')
            release_checks(db,current,identity_loader(db.connection()))
            db.rollback()
        dialect = db.get_bind().dialect.name
        if dialect == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        elif dialect == 'postgresql':
            db.execute(text("SET LOCAL lock_timeout='1000ms'"))
            # Monitor start uses SHARE membership fencing. Include empty sender
            # and power predicates to prevent inserts after final inspection.
            if action == 'release':
                db.execute(text('LOCK TABLE dispatch_attempt_receipts, dispatch_sender_registrations, education_monitor_claims, jobs, power_target_operations, print_jobs, printer_dispatch_claims, printers IN SHARE ROW EXCLUSIVE MODE NOWAIT'))
        else: refuse('unsupported_locking')
        graph = prepare_parent_graph(db,[before['job']['id']])
        live = rows(db,'SELECT id,username,role,group_id,is_active FROM users WHERE id=:id'+(' FOR UPDATE NOWAIT' if dialect=='postgresql' else ''),{'id':actor['id']})
        if (not live or not live[0]['is_active'] or live[0]['role']!='admin'
                or live[0]['group_id']!=actor.get('group_id')):
            refuse('not_found',404)
        actor = {**actor, **live[0]}
        if not education_authority(db,before,actor,lock=True): refuse('not_found',404)
        def lock(sql):
            db.execute(text(sql + (' FOR UPDATE NOWAIT' if dialect=='postgresql' else '')), {'pid':printer_id,'jid':before['job']['id']}).all()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — constant lock statements plus a fixed dialect suffix; values are bound
        # SQLite BEGIN IMMEDIATE already owns the writer. PostgreSQL follows
        # claim-before-printer for release, job-before-printer for admission/hold.
        if action != 'release':
            lock('SELECT id FROM jobs WHERE id=:jid')
            lock('SELECT id FROM printers WHERE id=:pid')
            lock('SELECT printer_id FROM printer_dispatch_claims WHERE printer_id=:pid')
        else:
            lock('SELECT printer_id FROM printer_dispatch_claims WHERE printer_id=:pid')
            lock('SELECT id FROM printers WHERE id=:pid')
        if action == 'release':
            lock('SELECT claim_token FROM dispatch_attempt_receipts WHERE printer_id=:pid ORDER BY claim_token')
            lock('SELECT id FROM print_jobs WHERE printer_id=:pid ORDER BY id')
            lock('SELECT id FROM jobs WHERE id=:jid')
        if registry.get_provider('OrderFulfillmentProvider') is not None:
            recheck_parent_graph(db,graph)
        else:
            from core.order_production import prepare_job_write
            prepare_job_write(db,[before['job']['id']])
        current = capture(db,printer_id,actor,identity_loader)
        if current is None or not hmac.compare_digest(request.attempt_handle,current['attempt']): refuse('attempt_changed')
        authorized(db,current,actor)
        if not hmac.compare_digest(request.snapshot_handle,current['snapshot']): refuse('snapshot_changed')
        job = current['job']
        if action == 'release':
            release_checks(db,current,identity_loader(db.connection()))
            release_evidence_checks(db,current)
            claim = current['claim']
            table = PrinterDispatchClaim.__table__
            values = {k:claim[k] for k in (*IDENTITY,'state')}
            predicate = [table.c[k].is_(None) if v is None else table.c[k]==v for k,v in values.items()]
            if db.execute(delete(table).where(*predicate)).rowcount != 1: refuse('attempt_changed')
        else:
            db.execute(text('UPDATE jobs SET hold=true WHERE id=:id'),{'id':job['id']})
            if action=='reconcile-retain' and request.education_action=='request_unbound_cancellation':
                if len(current['reservations'])!=1 or any(r['job_id']!=job['id'] for r in current['reservations']) or any(r['state'] not in ('reserved','awaiting') or any(o['scheduled_job_id']==r['job_id'] for o in current['observations']) for r in current['reservations']):
                    refuse('education_authority_unresolved')
        facts = dict(old_claim_state=current['claim']['state'] if current['claim'] else 'education_reservation_retained',
                     hold_before=bool(job['hold']),hold_after=True,receipt_present=bool(current['receipts']),
                     observations=current['observations'],
                     education=[{'submission_id':s['id'],'revision':s['lifecycle_revision'],'status':s['status']} for s in current['submissions']],
                     cancellation_intent=action=='reconcile-retain' and request.education_action=='request_unbound_cancellation',
                     attestation=request.attestation.model_dump() if action=='release' else None,
                     outcome='released' if action=='release' else 'held' if action=='hold' else 'retained')
        # Cancellation intent binds exact authority with a purpose-separated MAC;
        # token/digest never enter history. Revocation is deliberately separate.
        if action=='release':
            facts['verified_evidence'] = {'target_exclusion':'held_through_commit',
                'monitor_last_seen':str(current['printer']['last_seen']),
                'monitor_state':current['printer']['gcode_state'],
                'sender_power_histories':'resolved', 'snapshot_reference':current['snapshot']}
        if facts['cancellation_intent']:
            facts['education_attempt_handle']=current['attempt']
            facts['education_snapshot_handle']=current['snapshot']
            reservation=current['reservations'][0]
            from core.auth import SECRET_KEY
            binding=hmac.new(SECRET_KEY.encode(),b'ODIN recovery cancellation authority v1\0'+json.dumps(reservation,sort_keys=True,default=str).encode(),hashlib.sha256).hexdigest()
            facts['education_cancellation_intent']={'claim_id':reservation['claim_id'],
                'authority_revision':reservation['authority_revision'],'submission_id':reservation['submission_id'],
                'job_id':reservation['job_id'],'printer_id':reservation['printer_id'],'org_id':reservation['org_id'],
                'authority_handle':'ec1.'+binding,'submission_revision':current['submissions'][0]['lifecycle_revision']}
        audit = log_audit(db,'dispatch_recovery_'+action,'printer',printer_id,{'decision':action,'reason_category':request.reason_category})
        db.flush()
        audit_id = audit.id
        history = import_module('core.schema.migrations.038_dispatch_recovery').history
        result = db.execute(history.insert().values(audit_id=audit_id,actor_id=actor['id'],actor_identity=actor['username'],
            owner_org_id=current['printer']['org_id'],charged_org_id=job['charged_to_org_id'],printer_id=printer_id,job_id=job['id'],
            attempt_handle=current['attempt'],snapshot_handle=current['snapshot'],decision=action,reason_category=request.reason_category,
            facts=json.dumps(facts,sort_keys=True,default=str)))
        decision_id = result.inserted_primary_key[0]
        if handle and not handle.held(): refuse('evidence_unavailable')
        committing = True
        db.commit()
        if handle:
            closed = handle.close(); handle=None
            if not closed.released: refuse('outcome_unknown',503)
        committing = False
        return dict(outcome=facts['outcome'],changed=action=='release' or not bool(job['hold']),audit_id=audit_id,decision_id=decision_id,
                    held=True,claim_retained=action!='release',attempt_handle=current['attempt'],refresh_required=True,
                    guidance=GUIDANCE)
    except HTTPException as error:
        try: db.rollback()
        except Exception: pass
        if not isinstance(error.detail,dict) or 'code' not in error.detail:
            refuse('evidence_unavailable',503)
        raise
    except Exception as exc:
        try: db.rollback()
        except Exception: pass
        if committing: refuse('outcome_unknown',503)
        code = getattr(getattr(exc,'orig',None),'sqlstate',None)
        if code in ('55P03','40P01','40001') or 'locked' in str(exc).lower(): refuse('authority_busy')
        refuse('recovery_unavailable',503)
    finally:
        if handle is not None:
            try: handle.close()
            except Exception: pass  # Never replace a sanitized refusal with a driver error.
