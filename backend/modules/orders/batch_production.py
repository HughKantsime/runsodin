"""Immutable fixed-yield recipe revisions and explicit inspected acceptance.

No Square writes. Pending additions are immutable requests, not stock balances.
"""
from datetime import datetime, timezone
import hashlib
import importlib
import json
import uuid
from fastapi import HTTPException
from sqlalchemy import select, update, text, func
from sqlalchemy.exc import IntegrityError
from modules.orders import square_store as square
from modules.orders.square_client import SquareReader

schema = importlib.import_module('core.schema.migrations.035_production_acceptance')
recipes, receipts, additions = schema.TABLES


def packed(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def fail(message):
    raise HTTPException(409, message)


def job_evidence(db, owner, job_id, *, lock=False):
    params = {'id': job_id, 'owner': owner}
    if lock and db.execute(text('UPDATE jobs SET id=id WHERE id=:id AND ((:owner=0 AND charged_to_org_id IS NULL) OR (:owner>0 AND charged_to_org_id=:owner))'), params).rowcount != 1:
        raise HTTPException(404, 'Production job not found')
    row = db.execute(text('SELECT id,item_name,status,quantity,model_id,print_file_id,payload_sha256,plate_index,printer_id,economic_uid FROM jobs WHERE id=:id AND ((:owner=0 AND charged_to_org_id IS NULL) OR (:owner>0 AND charged_to_org_id=:owner))'), params).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, 'Production job not found')
    from core.interfaces.education_policy import assert_generic_model_reference_allowed
    assert_generic_model_reference_allowed(db, row['model_id'])
    if db.execute(text('SELECT 1 FROM education_submissions WHERE job_id=:id'), params).first():
        raise HTTPException(404, 'Production job not found')
    if row['status'] != 'completed' or row['quantity'] != 1 or not row['economic_uid']:
        fail('Choose a completed single-run job with retained identity evidence')
    if not row['model_id'] or not row['print_file_id'] or row['plate_index'] is None or not isinstance(row['payload_sha256'], str) or len(row['payload_sha256']) != 64:
        fail('The job has no exact file, plate and payload evidence')
    observed = db.execute(text('''SELECT r.claim_token,r.configuration_key,r.printer_id AS receipt_printer_id,p.id AS observation_id,p.economic_birth_nonce AS observation_uid,p.filename,p.job_name,r.observed_filename_key,
        p.status,p.scheduled_job_id,p.printer_id,p.ended_at FROM dispatch_attempt_receipts r
        LEFT JOIN print_jobs p ON p.id=r.observed_print_job_id WHERE r.job_id=:id'''), params).mappings().all()
    if len(observed) != 1:
        fail('This job has missing or multiple physical attempts. It needs a separate review.')
    physical = dict(observed[0])
    if (physical['status'] != 'completed' or physical['ended_at'] is None or not physical['observation_uid']
            or physical['scheduled_job_id'] != job_id or physical['printer_id'] != row['printer_id']
            or physical['receipt_printer_id'] != row['printer_id']):
        fail('A completed physical print is not unambiguously bound to this job')
    from core.dispatch_receipts import generic_observed_token, observed_filename_matches
    from core.auth import SECRET_KEY
    protocol=db.execute(text('SELECT api_type FROM printers WHERE id=:id'),{'id':row['printer_id']}).scalar_one()
    wire_name=physical['job_name'] if protocol=='bambu' else physical['filename']
    if (generic_observed_token(protocol,wire_name) != physical['claim_token'] or
            not observed_filename_matches(SECRET_KEY.encode(),physical['claim_token'],protocol,wire_name,physical['observed_filename_key'])):
        fail('Physical print correlation no longer matches the retained dispatch receipt')
    # Successful terminal retirement verifies the claim's payload SHA against
    # the retained job, receipt/observation binding and exact filename HMAC.
    # Sender registrations are optional fences, not retained print evidence.
    if db.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE job_id=:id'), params).first():
        fail('The physical dispatch claim has not been cleanly retired')
    senders=db.execute(text('SELECT claim_token,payload_sha256,owner_org_id,job_id,printer_id,configuration_key,state FROM dispatch_sender_registrations WHERE claim_token=:claim OR job_id=:id'),{'claim':physical['claim_token'],'id':job_id}).mappings().all()
    if len(senders)>1:
        fail('This job has multiple sender attempts. It needs a separate review.')
    sender=senders[0] if senders else None
    if sender is not None and (sender['state']!='quiesced' or any(sender[key] != expected for key,expected in (
            ('claim_token',physical['claim_token']),('payload_sha256',row['payload_sha256']),('owner_org_id',owner or None),
            ('job_id',job_id),('printer_id',row['printer_id']),('configuration_key',physical['configuration_key'])))):
        fail('Retained sender evidence conflicts with the exact printed payload')
    # Exclude a second unbound/failed attempt as well as multiple dispatch receipts.
    if db.execute(text('SELECT COUNT(*) FROM print_jobs WHERE scheduled_job_id=:id'), params).scalar_one() != 1:
        fail('This job has multiple physical attempts. It needs a separate review.')
    # Model/printer ownership must remain exact; shared resources are not stock authority.
    from modules.models_library.models import Model
    from modules.printers.models import Printer
    for model, identifier in [(Model,row['model_id']), (Printer,row['printer_id'])]:
        clause = model.org_id.is_(None) if owner == 0 else model.org_id == owner
        if lock:
            result = db.execute(update(model).where(model.id==identifier,clause).values(id=model.id))
            if result.rowcount != 1:
                fail('Recipe model or printer ownership changed')
        elif not db.execute(select(model.id).where(model.id==identifier,clause)).first():
            fail('Recipe model or printer ownership changed')
    return {**dict(row), **physical}


def recipe_payload(evidence):
    return {key: evidence[key] for key in ('model_id','print_file_id','payload_sha256','plate_index','printer_id','configuration_key')}


def recipe(db, owner, recipe_id):
    row = db.execute(select(recipes).where(recipes.c.id == recipe_id, recipes.c.owner == owner)).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, 'Recipe not found')
    return {'id': row['id'], **json.loads(row['definition'])}


def single_print_product(db, owner, product_id, model_id, kind):
    product = square.own_product(db,owner,product_id,lock=True)
    from modules.orders.models import ProductComponent
    # A mixed plate's qualified flush output can reference its own library model.
    # Multiple components, multiple parts or additional consumables still require finishing.
    components=db.execute(select(ProductComponent).where(ProductComponent.product_id==product_id).with_for_update()).scalars().all()
    if components and (len(components)!=1 or components[0].quantity_needed!=1 or (kind=='main' and components[0].model_id!=model_id)):
        fail('Assembly or multi-part products need a finishing workflow before stock acceptance')
    if db.execute(text('SELECT 1 FROM product_consumables WHERE product_id=:id LIMIT 1'),{'id':product_id}).first():
        fail('Products requiring additional consumables need a finishing workflow before stock acceptance')
    return product, (components[0].model_id if components else None)


def create_recipe(db, user, data):
    owner = square.tenant(user)
    square.lock_owner(db, owner, user)
    from modules.orders.finishing import lock as lock_finishing
    component_state,_=lock_finishing(db,owner,user)
    evidence = job_evidence(db, owner, data.reference_job_id, lock=True)
    keys = [(o.product_id,o.kind) for o in data.outputs]
    if len(set(keys)) != len(keys) or not any(o.kind == 'main' for o in data.outputs):
        fail('Use distinct product/output types and at least one main output')
    if db.execute(select(func.count()).select_from(recipes).where(recipes.c.owner == owner)).scalar_one() >= 1000:
        fail('Recipe limit reached. Review existing revisions before adding more.')
    outputs = []
    for output in sorted(data.outputs, key=lambda o: (o.product_id,o.kind)):
        product, bom_model_id = single_print_product(db, owner, output.product_id, evidence["model_id"],output.kind)
        outputs.append({**output.model_dump(), 'product_name': product.name,'bom_model_id':bom_model_id})
    if data.qualification_confirmed:
        from modules.orders.assortment import validate_qualified_outputs
        validate_qualified_outputs(db,owner,data.outputs)
    identifier = uuid.uuid4().hex
    definition = {'version':1, 'name':data.name.strip(), 'materials':data.materials.strip(),
                  'qualified':data.qualification_confirmed, 'created_at':now(),
                  'reference_job_id':data.reference_job_id, 'reference_claim_token':evidence['claim_token'],
                  'reference_observation_uid':evidence['observation_uid'],
                  'payload':recipe_payload(evidence), 'outputs':outputs}
    if not definition['name'] or not definition['materials']:
        fail('Enter a recipe name and the material/color requirements')
    db.execute(recipes.insert().values(id=identifier,owner=owner,definition=packed(definition)))
    db.commit()
    return {'id':identifier, **definition}


def receipt_public(row):
    return {'id':row['id'], 'recipe_id':row['recipe_id'], **json.loads(row['definition'])}


def inspect_batch(db, user, data):
    owner = square.tenant(user)
    square.lock_owner(db, owner, user)
    from modules.orders.finishing import lock as lock_finishing
    component_state,_=lock_finishing(db,owner,user)
    if data.inspection_confirmed is not True:
        fail('Inspect the physical pieces before accepting this batch')
    spec = recipe(db, owner, data.recipe_id)
    if spec['qualified'] is not True:
        fail('This recipe is not qualified. Create a qualified revision after verifying its yield.')
    evidence = job_evidence(db, owner, data.job_id, lock=True)
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
    try:
        db.execute(receipts.insert().values(id=identifier,owner=owner,job_uid=evidence['economic_uid'],
            claim_token=evidence['claim_token'],recipe_id=data.recipe_id,request_hash=request_hash,definition=packed(definition)))
        if changes:
            db.execute(additions.insert().values(id=uuid.uuid4().hex,owner=owner,receipt_id=identifier,
                destination=packed(destination),request_body=packed({'idempotency_key':identifier,'changes':changes}),
                state='prepared',attempts=0))
        from core.audit import log_audit
        log_audit(db, 'production.inspected', 'production_acceptance', None,
            {'receipt_id':identifier, 'job_id':data.job_id, 'operator_id':user['id'],
             'auth_kind':user.get('_auth_kind'), 'api_token_id':user.get('_token_id')})
        db.commit()
    except IntegrityError:
        db.rollback()
        fail('This physical batch was already recorded. Reload its inspection receipt.')
    return {'id':identifier,'recipe_id':data.recipe_id,**definition}


def assert_tenant_deletable(db, owner):
    if db.execute(select(receipts.c.id).where(receipts.c.owner == owner).limit(1)).first():
        fail('This organization has retained production inspection receipts. It cannot be deleted in this workflow; retain these business records for review.')


def assert_product_deletable(db, product):
    owner = product.org_id or 0
    definitions = db.execute(select(recipes.c.definition).where(recipes.c.owner==owner)).scalars()
    if any(any(o['product_id']==product.id for o in json.loads(value)['outputs']) for value in definitions):
        fail('This product belongs to a retained production recipe. Keep it to preserve batch identity.')
