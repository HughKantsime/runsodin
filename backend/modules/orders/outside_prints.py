"""Owner-attested outside prints. No dispatch authority or external writes."""
import json
import uuid
from sqlalchemy import text, select
from fastapi import HTTPException
from modules.orders import batch_production as batches, square_store as square

PREFIX = 'outside_print_binding:'


def audit(db, action, entity_type, entity_id, details):
    """Stage atomically with NULL actor columns; attester identity is in details."""
    from core.models import AuditLog
    from core.log_redaction import redact_structure
    db.execute(AuditLog.__table__.insert().values(action=action,entity_type=entity_type,
        entity_id=entity_id,details=redact_structure(details)))



def configuration_key(db, printer_id):
    # Use the same equality proof as dispatch; never expose configuration inputs.
    from core.interfaces.printer_binding import require_printer_binding_provider
    key = require_printer_binding_provider().configuration_key(db, printer_id)
    if key is None:
        batches.fail('Printer configuration cannot be verified')
    return key


def physical(db, owner, print_job_id, *, lock=False):
    params = {'id': print_job_id, 'owner': owner}
    query = text('''SELECT p.* FROM print_jobs p JOIN printers r ON r.id=p.printer_id
        WHERE p.id=:id AND ((:owner=0 AND r.org_id IS NULL) OR r.org_id=:owner)''')
    row = db.execute(query, params).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, 'Outside print not found')
    if lock:
        # Follow observed-start order: production parents, job, printer, print.
        # Recheck membership after locks rather than taking a new parent's lock.
        from core.order_production import prepare_job_write
        from core.dispatch_observation_executor import TransactionView
        from core.interfaces.printer_binding import require_printer_binding_provider, PrinterResourceRefused
        from sqlalchemy.exc import DBAPIError
        provider = require_printer_binding_provider()
        view = TransactionView(db)
        before = (row['scheduled_job_id'],row['printer_id'],row['economic_birth_nonce'])
        try:
            if before[0] is not None:
                prepare_job_write(db,[before[0]])
                provider.lock_resource(view,'jobs',before[0],'status')
            provider.lock_resource(view,'printers',before[1],'is_active')
            provider.lock_resource(view,'print_jobs',print_job_id,'status')
        except PrinterResourceRefused:
            batches.fail('Print resources changed. Reload before binding or inspecting')
        except DBAPIError as exc:
            if getattr(exc.orig,'sqlstate',None) == '55P03':
                batches.fail('Print resources are busy. Reload before binding or inspecting')
            raise
        row = db.execute(query,params).mappings().one_or_none()
        if row is None or (row['scheduled_job_id'],row['printer_id'],row['economic_birth_nonce']) != before:
            batches.fail('Print linkage changed. Reload before binding or inspecting this print')
    if row['status'] != 'completed' or row['ended_at'] is None or not row['economic_birth_nonce']:
        batches.fail('Choose a completed outside print with ended_at and retained physical identity')
    from core.interfaces.education_policy import is_education_reserved_filename
    if (any(is_education_reserved_filename(row[k]) for k in ('filename','job_name')) or
            db.execute(text('SELECT 1 FROM education_submissions WHERE job_id=:job'), {'job':row['scheduled_job_id']}).first()):
        batches.fail('Education prints cannot be outside-bound')
    if db.execute(text('SELECT 1 FROM dispatch_attempt_receipts WHERE observed_print_job_id=:id'), params).first():
        batches.fail('ODIN-dispatched prints must use the normal inspection path')
    if any('odin-dispatch-' in (row[k] or '').lower() for k in ('filename','job_name')):
        batches.fail('Reserved ODIN dispatch filenames cannot be outside-bound')
    return dict(row)


def load(db, owner, print_job_id):
    value = db.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key':PREFIX+str(print_job_id)}).scalar_one_or_none()
    if value is None:
        batches.fail('This print has no owner-attested outside binding')
    binding = value if isinstance(value,dict) else json.loads(value)
    if binding['owner'] != owner:
        raise HTTPException(404, 'Outside print not found')
    return binding


def evidence(db, owner, print_job_id, *, lock=False):
    row = physical(db, owner, print_job_id, lock=lock)
    binding = load(db, owner, print_job_id)
    if (row['economic_birth_nonce'] != binding['physical_uid'] or row['economic_uid'] != binding['economic_uid']
            or row['scheduled_job_id'] is not None or row['printer_id'] != binding['payload']['printer_id']
            or row['filename'] != binding['filename'] or row['job_name'] != binding['job_name']
            or str(row['ended_at']) != binding['ended_at']):
        batches.fail('Outside print evidence changed; review the retained binding')
    if configuration_key(db,row['printer_id']) != binding['payload']['configuration_key']:
        batches.fail('Outside print printer configuration changed')
    return {**binding['payload'], 'economic_uid':binding['acceptance_uid'],
            'observation_uid':binding['physical_uid'], 'claim_token':binding['id'],
            'ended_at':row['ended_at'], 'outside_binding':binding}


def bind(db, user, print_job_id, data):
    from modules.orders.production_auth import assert_production_operator
    assert_production_operator(user)
    owner = square.tenant(user)
    square.lock_owner(db, owner, user)
    if not (data.attestation.started_outside_odin is True and data.attestation.file_revision_confirmed is True):
        batches.fail('Confirm the print was started outside ODIN and its file revision')
    from modules.orders.finishing import lock as lock_finishing, component_for_job
    component_state,_ = lock_finishing(db,owner,user)
    row = physical(db, owner, print_job_id, lock=True)
    if db.execute(text('SELECT 1 FROM system_config WHERE key=:key'), {'key':PREFIX+str(print_job_id)}).first():
        batches.fail('This outside print is already bound')
    spec = batches.recipe(db, owner, data.recipe_id)
    if spec['qualified'] is not True:
        batches.fail('Choose a qualified recipe revision')
    from core.interfaces.education_policy import assert_generic_model_reference_allowed
    assert_generic_model_reference_allowed(db, spec['payload']['model_id'])
    if spec['payload']['printer_id'] != row['printer_id'] or spec['payload']['configuration_key'] != configuration_key(db,row['printer_id']):
        batches.fail('Recipe printer or configuration does not match this print')
    # Birth nonce is always present, including financially unresolved outside prints.
    # Never rewrite their immutable economic ownership history.
    acceptance_uid = row['economic_uid'] or row['economic_birth_nonce']
    candidates = [acceptance_uid, row['economic_birth_nonce']]
    job = None
    if row['scheduled_job_id']:
        job = db.execute(text('SELECT * FROM jobs WHERE id=:id'), {'id':row['scheduled_job_id']}).mappings().one_or_none()
        if job is None:
            batches.fail('Linked job no longer exists; separate review is required')
        metrics_only = not any(job[k] for k in ('model_id','print_file_id','order_item_id','economic_uid','charged_to_org_id','charged_to_user_id','submitted_by'))
        if not metrics_only and (job['charged_to_org_id'] or 0) != owner:
            batches.fail('Linked job ownership differs from this print')
        from core.registry import registry
        policy = registry.get_provider('EducationPolicyProvider')
        if policy is None:
            raise HTTPException(503,'Education policy is unavailable for linked-job restoration')
        try:
            policy.assert_generic_job_mutation_allowed(db,job['id'])
        except HTTPException as exc:
            if exc.status_code in (403,404):
                batches.fail('Education prints cannot be outside-bound')
            raise
        candidates.append(job['economic_uid'])
    for receipt in db.execute(select(batches.receipts)).mappings():
        definition = json.loads(receipt['definition'])
        if receipt['job_uid'] in candidates or definition.get('physical_uid') == row['economic_birth_nonce']:
            batches.fail('This physical attempt already has production acceptance')
    if any(uid and component_for_job(component_state,uid) for uid in candidates):
        batches.fail('This physical attempt already has component acceptance')
    restored = None
    if job is not None:
        # A retained receipt with no observed print is recovery history, not proof
        # this outside plate was dispatched. Active senders/claims still fence it.
        if db.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE job_id=:id'), {'id':job['id']}).first():
            batches.fail('Linked job still has an active dispatch claim')
        if db.execute(text("SELECT 1 FROM dispatch_sender_registrations WHERE job_id=:id AND state<>'quiesced'"), {'id':job['id']}).first():
            batches.fail('Linked job sender has not quiesced')
        if db.execute(text('SELECT 1 FROM print_jobs WHERE scheduled_job_id=:job AND id<>:id'), {'job':job['id'],'id':print_job_id}).first():
            batches.fail('Linked job has other physical attempts and needs separate review')
        if db.execute(text('SELECT 1 FROM dispatch_attempt_receipts WHERE job_id=:id AND observed_print_job_id IS NOT NULL'), {'id':job['id']}).first():
            batches.fail('Linked job has a dispatched physical attempt')
        from modules.jobs.services import transition_job
        previous = db.execute(text('SELECT value FROM system_config WHERE key=:key'),
            {'key':f'outside_print_previous_job:{print_job_id}'}).scalar_one_or_none()
        previous = (previous if isinstance(previous,dict) else json.loads(previous)) if previous else None
        if previous and previous['job_id'] == job['id']:
            state = previous['state']
        elif job['hold']:
            # Pre-fix held work (job 70) was pending, never dispatched.
            state = {'status':'pending','hold':True,'actual_start':None,'actual_end':None,'duration_hours':None,'queue_position':None}
        elif metrics_only:
            # The monitor created this metrics row at print completion; there
            # was no queued job or earlier state to restore.
            state = None
        else:
            batches.fail('Linked job has no retained pre-match state; separate review is required')
        if state is not None:
            status = state['status']
            if not transition_job(db,job['id'],status,'outside_binding',from_states=('completed',),
                                  extra_fields={k:v for k,v in state.items() if k!='status'}):
                batches.fail('Linked job changed during restoration')
        from core.print_attempts import detach_print_attempt, sqlalchemy_runner
        detach_print_attempt(sqlalchemy_runner(db),print_job_id,job['id'])
        db.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=:id'), {'id':print_job_id})
        from core.order_production import prepare_job_write, reconcile_job_write
        reconcile_job_write(db,prepare_job_write(db,[job['id']]))
        restored = {'job_id':job['id'],'previous_status':job['status'],
                    'status':state['status'] if state else job['status'],
                    'hold':bool(state['hold']) if state else bool(job['hold']), 'metrics_only':metrics_only,
                    'restored_state':state}
    archives = db.execute(text('SELECT id FROM print_archives WHERE print_job_id=:id ORDER BY id'), {'id':print_job_id}).scalars().all()
    binding = {'version':1,'id':uuid.uuid4().hex,'owner':owner,'print_job_id':print_job_id,
        'economic_uid':row['economic_uid'],'acceptance_uid':acceptance_uid,'physical_uid':row['economic_birth_nonce'],
        'archive_id':archives[0] if len(archives)==1 else None,'archive_ids':archives,
        'recipe_id':spec['id'],'recipe_revision':spec,'payload':spec['payload'],
        'filename':row['filename'],'job_name':row['job_name'],'ended_at':str(row['ended_at']),
        'odin_dispatched':False,'owner_attested':True,'attestation':data.attestation.model_dump(),
        'attested_by':data.attested_by,'actor_user_id':user['id'],'actor_token_id':user.get('_token_id'),
        'created_at':batches.now(),'restored_job':restored}
    db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'), {'key':PREFIX+str(print_job_id),'value':batches.packed(binding)})
    db.execute(text('DELETE FROM system_config WHERE key=:key'), {'key':f'outside_print_previous_job:{print_job_id}'})
    audit(db,'production.outside_print_bound','print_job',print_job_id,binding)
    db.commit()
    return binding


def eligible(db, owner, offset):
    result=[]
    rows=db.execute(text('SELECT value FROM system_config WHERE key LIKE :prefix ORDER BY key LIMIT 100 OFFSET :offset'), {'prefix':PREFIX+'%','offset':offset}).scalars().all()
    for value in rows:
        binding=value if isinstance(value,dict) else json.loads(value)
        if binding['owner'] != owner: continue
        try:
            proof=evidence(db,owner,binding['print_job_id'])
        except HTTPException as exc:
            if exc.status_code in (403,404,409): continue
            raise
        result.append({'kind':'outside_print','print_job_id':binding['print_job_id'],
            'name':binding['job_name'] or binding['filename'],'recipe':binding['recipe_revision'],
            'payload':batches.recipe_payload(proof)})
    return result, len(rows)==100


# Acceptance rules intentionally mirror the unchanged ODIN receipt path.
from modules.orders.batch_production import (now, fail, packed, recipe, recipe_payload,
    single_print_product, receipt_public, receipts, additions)
from modules.orders.square_client import SquareReader
from sqlalchemy.exc import IntegrityError
import hashlib
outside_evidence = evidence


def inspect(db, user, data):
    owner = square.tenant(user)
    square.lock_owner(db, owner, user)
    from modules.orders.finishing import lock as lock_finishing
    component_state,_=lock_finishing(db,owner,user)
    if data.inspection_confirmed is not True:
        fail('Inspect the physical pieces before accepting this batch')
    spec = recipe(db, owner, data.recipe_id)
    if spec['qualified'] is not True:
        fail('This recipe is not qualified. Create a qualified revision after verifying its yield.')
    evidence = outside_evidence(db, owner, data.print_job_id, lock=True)
    if evidence['outside_binding']['recipe_id'] != data.recipe_id:
        fail('Inspection recipe differs from the outside binding')
    from core.interfaces.education_policy import assert_generic_model_reference_allowed
    assert_generic_model_reference_allowed(db, evidence['model_id'])
    from modules.models_library.models import Model
    from modules.printers.models import Printer
    from sqlalchemy import update
    for model, identifier in [(Model,evidence['model_id']), (Printer,evidence['printer_id'])]:
        clause = model.org_id.is_(None) if owner == 0 else model.org_id == owner
        if db.execute(update(model).where(model.id==identifier,clause).values(id=model.id)).rowcount != 1:
            fail('Recipe model or printer ownership changed')
    request_hash = hashlib.sha256(packed({'recipe_id':data.recipe_id,'job_uid':evidence['economic_uid'],
        'outputs':sorted([o.model_dump(exclude_none=True) for o in data.outputs],key=lambda o:(o['product_id'],o['kind']))}).encode()).hexdigest()
    from modules.orders.finishing import component_for_job
    if component_for_job(component_state,evidence['economic_uid']):
        fail('This job is already inspected as components. Finish the assembly instead of counting it again.')
    previous = db.execute(select(receipts).where(receipts.c.job_uid == evidence['economic_uid'])).mappings().one_or_none()
    if previous:
        if previous['owner'] != owner or previous['request_hash'] != request_hash:
            fail('This physical batch already has a different inspection receipt')
        return receipt_public(previous)
    if recipe_payload(evidence) != spec['payload']:
        fail('Job file, plate, model or printer configuration does not match this recipe revision')
    expected = {(o['product_id'],o['kind']):o for o in spec['outputs']}
    supplied = {(o.product_id,o.kind):o for o in data.outputs}
    if len(supplied) != len(data.outputs) or set(expected) != set(supplied):
        fail('Inspect each main and flush output exactly once')
    # Keep the existing product-before-connector lock order used by mapping
    # writers and recovery, including global owner 0 on PostgreSQL.
    products = {}
    for key in sorted(expected):
        output, counted = expected[key], supplied[key]
        product, bom_model_id = single_print_product(db, owner, output['product_id'], evidence['model_id'],output['kind'])
        if product.name != output['product_name'] or bom_model_id != output['bom_model_id']:
            fail('A recipe product changed. Verify its identity and create a new recipe revision.')
        if counted.accepted + counted.rejected != output['expected']:
            fail('Accepted plus rejected must equal the recipe yield for every output')
        products[key] = product
    accepted_outputs, quantities = [], {}
    doc = None
    if any(o.accepted for o in data.outputs):
        doc, encoded = square.load(db, owner)
        if not doc:
            fail('Map accepted products to Square before recording this inspection')
        square.check_revision(doc, data.square_revision)
        doc, _ = square.fence(db, owner, doc, encoded)
    from modules.orders.stock_baseline import utc
    observed_at = utc(evidence['ended_at']).isoformat()
    for key in sorted(expected):
        output, counted, product = expected[key], supplied[key], products[key]
        mapping = doc['mappings'].get(str(product.id)) if doc else None
        if counted.accepted and not mapping:
            fail('Every accepted output needs an explicit Square mapping')
        baseline = mapping.get('stock_baseline') if mapping else None
        disposition = 'send'
        if counted.accepted and baseline:
            post_baseline = utc(observed_at) > utc(baseline['at'])
            if post_baseline:
                if counted.baseline_disposition == 'in_baseline':
                    fail('Conflicting baseline choice for a post-baseline observation; review this batch')
            elif counted.baseline_disposition is None:
                fail('Choose counted in baseline or not counted, send for this output')
            else:
                disposition = counted.baseline_disposition
        elif counted.baseline_disposition == 'in_baseline':
            fail('Cannot count an output in a baseline that does not exist')
        send_quantity = counted.accepted if disposition == 'send' else 0
        accepted_outputs.append({**output, 'accepted':counted.accepted,'rejected':counted.rejected,
            'disposition': disposition, 'send_quantity': send_quantity, 'observation_ended_at': observed_at,
            'baseline_version': baseline['version'] if baseline else None,
            'stock_baseline': dict(baseline) if baseline else None})
        if send_quantity:
            quantities[product.id] = quantities.get(product.id,0) + send_quantity
    destination, changes = None, []
    timestamp = now()
    identifier = uuid.uuid5(uuid.NAMESPACE_URL, f"odin:acceptance:v1:{owner}:{evidence['observation_uid']}:{evidence['claim_token']}").hex
    if quantities:
        destination = {'merchant_id':doc['merchant_id'],'environment':doc['environment']}
        for product_id, quantity in sorted(quantities.items()):
            mapping = doc['mappings'].get(str(product_id))
            if not mapping:
                fail('Every accepted output needs an explicit Square mapping')
            # Mapping was verified against live tracked variation on save. Freeze it;
            # do not call external services while this acceptance transaction holds locks.
            changes.append({'type':'ADJUSTMENT','adjustment':{
                'catalog_object_id':mapping['variation_id'],'from_location_id':mapping['location_id'],'to_location_id':mapping['location_id'],
                'from_state':'NONE','to_state':'IN_STOCK','quantity':str(quantity),
                'occurred_at':timestamp,'reference_id':identifier}})
    definition = {'version':1,'job_id':data.job_id,'job_uid':evidence['economic_uid'],
        'physical_uid':evidence['observation_uid'],'occurred_at':timestamp,'recipe_name':spec['name'],
        'outputs':accepted_outputs,'stock_status':'prepared' if quantities else ('in_baseline' if any(o['accepted'] for o in accepted_outputs) else 'no_sellable_output')}
    definition.update({'print_job_id':data.print_job_id,'odin_dispatched':False,'owner_attested':True,
        'outside_binding':evidence['outside_binding']})
    try:
        db.execute(receipts.insert().values(id=identifier,owner=owner,job_uid=evidence['economic_uid'],
            claim_token=evidence['claim_token'],recipe_id=data.recipe_id,request_hash=request_hash,definition=packed(definition)))
        if changes:
            db.execute(additions.insert().values(id=uuid.uuid4().hex,owner=owner,receipt_id=identifier,
                destination=packed(destination),request_body=packed({'idempotency_key':identifier,'changes':changes}),
                state='prepared',attempts=0))
        audit(db, 'production.inspected', 'production_acceptance', None,
            {'receipt_id':identifier, 'print_job_id':data.print_job_id, 'binding_id':evidence['claim_token'], 'actor_user_id':user['id'],
             'auth_kind':user.get('_auth_kind'), 'actor_token_id':user.get('_token_id')})
        db.commit()
    except IntegrityError:
        db.rollback()
        fail('This physical batch was already recorded. Reload its inspection receipt.')
    return {'id':identifier,'recipe_id':data.recipe_id,**definition}

