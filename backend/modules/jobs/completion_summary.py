"""Durable completion content and deterministic, read-only queue advice.

This path never performs a Square read or calls assortment.preview.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from fastapi import HTTPException
from sqlalchemy import select, text
from core.job_timeline import utc, history_complete
from modules.orders import batch_production as batches, assortment, square_store

BED_LINE = "ODIN has not checked the bed. Use 'Bed clear, start next' to start."


def clean(value, limit=160):
    return re.sub(r'[\x00-\x1f\x7f<>]', '', str(value or ''))[:limit]


def matching_recipes(db, owner, payload):
    result = []
    for row in db.execute(select(batches.recipes).where(batches.recipes.c.owner == owner)).mappings():
        definition = json.loads(row['definition'])
        if definition.get('qualified') is True and definition.get('payload') == batches.recipe_payload(payload):
            result.append({'id': row['id'], **definition})
    return result


def snapshot(db, owner, product_id, at):
    doc, _ = square_store.load(db, owner)
    mapping = (doc or {}).get('mappings', {}).get(str(product_id))
    if not mapping:
        return None
    row = db.execute(text('SELECT quantity,calculated_at,read_at FROM stock_snapshots WHERE owner_scope=:scope AND owner_key=:owner AND variation=:variation AND location=:location'),
        dict(scope='org' if owner else 'global', owner=owner, variation=mapping['variation_id'], location=mapping['location_id'])).mappings().first()
    if row is None:
        return None
    try:
        # Freshness belongs to the actual count read; calculated_at may be
        # older when the inventory quantity has not changed.
        if not timedelta(0) <= at-utc(row['read_at']) <= timedelta(hours=1) or utc(row['calculated_at']) > at:
            return None
        quantity = Decimal(row['quantity'])
        if not quantity.is_finite() or quantity < 0 or quantity != quantity.to_integral_value():
            return None
        return {'quantity': int(quantity), 'calculated_at': str(row['calculated_at']), 'read_at': str(row['read_at'])}
    except (ValueError, TypeError, InvalidOperation):
        return None


def finished_attempt(db, printer_id):
    return db.execute(text("SELECT economic_uid,ended_at FROM print_jobs WHERE printer_id=:id AND ended_at IS NOT NULL AND status IN ('completed','failed','cancelled') ORDER BY ended_at DESC,id DESC LIMIT 1"), {'id': printer_id}).mappings().first()


def configuration_key(db, printer_id):
    # Use the normal dispatch configuration identity; only its opaque key leaves
    # this function. No credentials are persisted in suggestions or fingerprints.
    from modules.printers.routes_dispatch_recovery import configured_identity
    identity = configured_identity(printer_id, db.connection() if callable(getattr(db, 'connection', None)) else db)
    return identity.configuration_key if identity else None


def material_facts(db, job, printer, finished, at, config):
    slots = [dict(r) for r in db.execute(text('SELECT slot_number,filament_type,color,color_hex,loaded_at FROM filament_slots WHERE printer_id=:id ORDER BY slot_number'), {'id': printer.id}).mappings()]
    requirements = {'material': getattr(job.filament_type, 'value', job.filament_type) or
                    getattr(getattr(job.model, 'default_filament_type', None), 'value', getattr(job.model, 'default_filament_type', None)),
                    'colors': job.colors_list, 'ams_mapping': job.ams_mapping}
    from core.print_file_binding import job_file_id
    file_id = job_file_id(db, job.model_id, job.print_file_id)
    raw = db.execute(text('SELECT filaments_json FROM print_files WHERE id=:id'), {'id': file_id}).scalar_one_or_none() if file_id else None
    try: source = json.loads(raw) if raw else []
    except (ValueError, TypeError): source = None
    required = []
    if isinstance(source, list) and source:
        for index, item in enumerate(source):
            if not isinstance(item, dict):
                required.append({'material': None, 'color': None})
            else:
                required.append({'material': getattr(job.filament_type, 'value', job.filament_type) or item.get('type'),
                    'color': requirements['colors'][index] if index < len(requirements['colors']) else item.get('color')})
    elif source == []:
        required = [{'material': requirements['material'], 'color': color} for color in requirements['colors']]
    requirements['filaments'] = required
    end = utc(finished['ended_at']) if finished else None
    ams = db.execute(text("SELECT g.completed_at,g.configuration_key FROM ams_current_reads c JOIN ams_read_generations g ON g.generation_id=c.generation_id WHERE c.printer_id=:id AND g.state='ready'"), {'id': printer.id}).mappings().first()
    # Bambu slot facts need a current AMS acquisition; passive old slots are not proof.
    if printer.api_type == 'bambu':
        fresh = bool(ams and config and ams['configuration_key'] == config and end and
                     utc(ams['completed_at']) > end and timedelta(0) <= at-utc(ams['completed_at']) <= timedelta(minutes=10))
    else:
        fresh = bool(end and slots)
    facts = {'required': requirements, 'slots': [{**s, 'loaded_at': str(s['loaded_at'])} for s in slots],
             'read_at': str(ams['completed_at']) if ams else None}
    if not fresh or not required or any(not r['material'] or not r['color'] for r in required):
        return 'verify_needed', [], facts
    changes = []
    mapping = job.ams_mapping
    if isinstance(mapping, str):
        try: mapping = json.loads(mapping)
        except ValueError: return 'verify_needed', [], facts
    from core.material_identity import canonical_reported_material
    def same_color(wanted, slot):
        def normalized(value):
            value = str(value or '').strip().casefold()
            hexadecimal = value.lstrip('#')
            return hexadecimal[:6] if re.fullmatch(r'[0-9a-f]{6}([0-9a-f]{2})?', hexadecimal) else value
        return normalized(wanted) in {normalized(slot['color']), normalized(slot['color_hex'])}
    for index, requirement in enumerate(required):
        color, material = requirement['color'], requirement['material']
        target = mapping[index]+1 if isinstance(mapping, list) and index < len(mapping) else None
        possible = [s for s in slots if target is None or s['slot_number'] == target]
        if canonical_reported_material(material) is None or not possible:
            return 'verify_needed', [], facts
        matches = [s for s in possible if
                   canonical_reported_material(s['filament_type']) == canonical_reported_material(material) and
                   same_color(color, s)]
        slot_fresh = lambda s: printer.api_type == 'bambu' or bool(s['loaded_at'] and end < utc(s['loaded_at']) <= at)
        if any(slot_fresh(s) for s in matches):
            continue
        if any(not slot_fresh(s) or not (s['color'] or s['color_hex']) or
               (canonical_reported_material(s['filament_type']) is None and str(s['filament_type']).lower() != 'empty')
               for s in possible):
            return 'verify_needed', [], facts
        changes.append({'slot': target or possible[0]['slot_number'], 'material': material, 'color': color})
    return ('change_needed' if changes else 'no_change'), changes, facts


def eligible(db, job, printer):
    from modules.jobs.scheduler import _same_printer_model, _printer_identity
    from core.dispatch_identity import ownership_refusal
    from core.print_file_binding import job_file_id, target_printer
    from types import SimpleNamespace
    from core.model_preview_access import is_education_owned_file
    if not printer.is_active or job.printer_id not in (None, printer.id) or job.effective_duration is None:
        return False
    if job.model is None:
        return False
    file_id = job_file_id(db, job.model_id, job.print_file_id)
    file_row = db.execute(text('SELECT * FROM print_files WHERE id=:id'), {'id': file_id}).mappings().first() if file_id else None
    file = SimpleNamespace(**dict(file_row)) if file_row else None
    if not file or not file.stored_path or target_printer(db, file_id) not in (None, printer.id):
        return False
    if ownership_refusal({'charged_to_org_id': job.charged_to_org_id, 'model_org_id': job.model.org_id,
                          'print_file_org_id': file.org_id},
                         {'org_id': printer.org_id, 'shared': printer.shared}):
        return False
    education = db.execute(text('SELECT 1 FROM education_submissions WHERE job_id=:id'), {'id': job.id}).first()
    if education:
        from core.registry import registry
        from modules.printers.services import evaluate_submission_compatibility
        policy = registry.get_provider('EducationPolicyProvider')
        context = policy.scheduler_context(db, job_id=job.id) if policy else None
        if not context or not context.get('authorized') or context.get('approved_printer_id') != printer.id:
            return False
        if not evaluate_submission_compatibility(db, org_id=context['org_id'], print_file_id=file_id, printer_id=printer.id).get('compatible'):
            return False
        from modules.jobs.education_order import head_refusal
        connection = db.connection() if callable(getattr(db, 'connection', None)) else db
        if head_refusal(connection, {'id':job.id, 'printer_id':printer.id, 'education_submission_id':context['id']}):
            return False
    else:
        from modules.jobs.pinning import job_pin, job_issue, BLOCKING
        from core.scheduler_payload_authority import capture
        if job_pin(job) not in (None, printer.id) or job_issue(db, job) in BLOCKING or capture(db, job.id) is None:
            return False
        from core.interfaces.education_policy import assert_generic_model_reference_allowed
        try: assert_generic_model_reference_allowed(db, job.model_id)
        except HTTPException: return False
        if is_education_owned_file(db, file_id, file.stored_path):
            return False
    required_model = getattr(file, 'printer_model', None)
    if required_model and not _same_printer_model(_printer_identity(printer), required_model):
        return False
    if job.required_tags and not all(t in (printer.tags or []) for t in job.required_tags):
        return False
    if job.target_type == 'model' and job.target_filter and not _same_printer_model(_printer_identity(printer), job.target_filter):
        return False
    if job.target_type == 'protocol' and job.target_filter and printer.api_type.casefold() != job.target_filter.casefold():
        return False
    return True


def next_suggestion(db, printer, *, visible=None, at=None):
    from modules.jobs.models import Job
    from modules.orders import show_fulfillment
    at = at or datetime.now(timezone.utc)
    finished = finished_attempt(db, printer.id)
    config = configuration_key(db, printer.id)
    candidates = db.query(Job).filter(Job.status == 'pending', Job.hold.is_(False)).order_by(
        Job.priority, Job.queue_position.is_(None), Job.queue_position, Job.created_at, Job.id).all()
    from modules.jobs.education_order import education_order
    education_ids = set(db.execute(text('SELECT job_id FROM education_submissions')).scalars())
    candidates = education_order(candidates, education_ids)
    ranked = []
    for scheduler_index, job in enumerate(candidates):
        if visible is not None and not visible(job):
            continue
        if not eligible(db, job, printer):
            continue
        owner = job.charged_to_org_id or 0
        payload = {k: getattr(job, k) for k in ('model_id', 'print_file_id', 'payload_sha256', 'plate_index')}
        payload.update(printer_id=printer.id, configuration_key=config)
        recipes = matching_recipes(db, owner, payload)
        shortfall = 0
        stock_inputs = []
        if len(recipes) == 1:
            policy, _ = assortment.load(db, owner)
            for output in recipes[0]['outputs']:
                if output['kind'] != 'main': continue
                rule = assortment.rule(policy['products'].get(str(output['product_id'])))
                stock = snapshot(db, owner, output['product_id'], at) if rule['routine_replenishment'] else None
                stock_inputs.append({'product_id': output['product_id'], 'snapshot': stock})
                if stock and rule['trigger_below'] is not None:
                    shortfall = max(shortfall, rule['trigger_below']-stock['quantity'])
        order_time = None
        order = job.order_item.order if job.order_item else None
        if order and (order.org_id or 0) == owner and getattr(order.status, 'value', order.status) not in ('shipped','delivered','cancelled','refunded'):
            doc, _ = show_fulfillment.load(db, order)
            if order.platform == 'wholesale' or show_fulfillment.summary(db, order, doc)['state'] == 'paid_committed':
                order_time = utc(order.created_at).timestamp()
        reason = 'Main product below minimum' if shortfall > 0 else 'Paid or wholesale open order' if order_time is not None else 'Scheduler queue order'
        rank = (0 if shortfall > 0 else 1, -shortfall, 0 if order_time is not None else 1,
                order_time if order_time is not None else float('inf'), scheduler_index, job.id)
        ranked.append((rank, job, reason, stock_inputs, str(order.created_at) if order_time is not None else None))
    if not ranked:
        return {'job_id': None, 'reason': 'No queued job fits this printer.', 'filament': 'verify_needed', 'fingerprint': None}
    _, job, reason, stock_inputs, order_created_at = min(ranked, key=lambda item: item[0])
    filament, changes, facts = material_facts(db, job, printer, finished, at, config)
    fingerprint = hashlib.sha256(batches.packed({'job_id': job.id, 'updated_at': str(job.updated_at),
        'printer_id': job.printer_id, 'payload_sha256': job.payload_sha256, 'plate': job.plate_index, 'materials': facts['required'],
        'configuration_key': config, 'finished_attempt_uid': finished['economic_uid'] if finished else None}).encode()).hexdigest()
    if not finished or not finished.get('economic_uid') or not config:
        fingerprint = None
    return {'job_id': job.id, 'reason': reason, 'filament': filament, 'load': changes,
            'fingerprint': fingerprint, 'finished_attempt_uid': finished['economic_uid'] if finished else None,
            'inputs': {'computed_at': at.isoformat(), 'job_updated_at': str(job.updated_at), 'material': facts, 'stock': stock_inputs, 'order_created_at': order_created_at}}


def build_summary(db, attempt_uid):
    row = db.execute(text('SELECT p.*,s.job_id AS summary_job_id,s.org_id AS summary_org_id,r.name AS printer_name FROM job_summary_outbox s JOIN print_jobs p ON p.economic_uid=s.print_attempt_uid AND p.economic_birth_nonce=s.attempt_birth LEFT JOIN printers r ON r.id=s.printer_id WHERE s.print_attempt_uid=:uid'), {'uid': attempt_uid}).mappings().one()
    job_id, owner = row['summary_job_id'], row['summary_org_id'] or 0
    job = db.execute(text('SELECT * FROM jobs WHERE id=:id'), {'id': job_id}).mappings().one()
    receipt_config = db.execute(text('SELECT configuration_key FROM dispatch_attempt_receipts WHERE observed_print_job_id=:id'), {'id': row['id']}).scalar_one_or_none()
    payload = {**dict(job), 'configuration_key': receipt_config}
    recipes = matching_recipes(db, owner, payload)
    recipe = recipes[0] if len(recipes) == 1 else None
    inspection_reason = None
    try: batches.job_evidence(db, owner, job_id)
    except HTTPException as exc: inspection_reason = str(exc.detail)
    previous_end = db.execute(text('SELECT MAX(ended_at) FROM print_jobs WHERE scheduled_job_id=:job AND id<>:id AND ended_at<=:start'),
        {'job':job_id,'id':row['id'],'start':row['started_at']}).scalar_one_or_none()
    # Dispatch/gate events precede physical observation and therefore have no
    # physical UID yet. Bound them to this job's interval after its prior attempt.
    timeline = [dict(r) for r in db.execute(text('SELECT kind,code,detail,occurred_at FROM job_timeline_events WHERE print_attempt_uid=:uid OR (print_attempt_uid IS NULL AND job_id=:job AND occurred_at>:lower AND occurred_at<=:end) ORDER BY occurred_at,id'),
        {'uid': attempt_uid, 'job':job_id,'lower':previous_end or job['created_at'],'end':row['ended_at']}).mappings()]
    complete = bool(row['started_at'] and row['ended_at'] and history_complete(db, row['printer_id'], row['started_at'], row['ended_at']))
    faults = [r for r in timeline if r['kind'] in ('paused','runout','hms','gate_refused','dispatch_failed')]
    health = 'history incomplete' if not complete else ('attention required' if faults else 'none')
    projections = []
    if recipe:
        yields = {}
        for output in recipe['outputs']:
            yields[output['product_id']] = yields.get(output['product_id'], 0) + output['expected']
        for product_id, expected_count in yields.items():
            stock = snapshot(db, owner, product_id, datetime.now(timezone.utc))
            projections.append({'product_id': product_id, 'quantity': stock['quantity']+expected_count if stock else None})
    from modules.printers.models import Printer
    printer = db.get(Printer, row['printer_id']) if row['printer_id'] else None
    suggestion = next_suggestion(db, printer, visible=lambda j: (j.charged_to_org_id or 0) == owner) if printer else None
    duration = max(0, (utc(row['ended_at'])-utc(row['started_at'])).total_seconds()) if row['started_at'] and row['ended_at'] else None
    summary = {'version': 1, 'print_attempt_uid': attempt_uid, 'printer_id': row['printer_id'], 'printer': clean(row['printer_name']),
        'job_id': job_id, 'job_name': clean(job['item_name']), 'actual_print_seconds': duration,
        'recipe_state': 'matched' if recipe else 'Recipe ambiguous' if recipes else 'No qualified recipe',
        'recipe_id': recipe['id'] if recipe else None, 'product': [clean(o['product_name']) for o in recipe['outputs'] if o['kind']=='main'] if recipe else [],
        'expected_outputs': recipe['outputs'] if recipe else [], 'health': health,
        'health_details': {'pauses': sum(e['kind']=='paused' for e in faults), 'runouts': sum(e['kind']=='runout' for e in faults),
                           'retries': max(sum(e['kind']=='dispatch_failed' for e in faults), sum(e['kind']=='dispatch_uploading' for e in timeline)-1, 0)},
        'health_events': [{**e, 'occurred_at': str(e['occurred_at']), 'detail': json.loads(e['detail'])} for e in faults],
        'do': 'Clear the plate, count, bin, and record the inspection.', 'inspection_url': f'/products?inspect={job_id}',
        'inspection_available': inspection_reason is None and recipe is not None,
        'inspection_unavailable_reason': inspection_reason or (None if recipe else 'Recipe ambiguous' if recipes else 'No qualified recipe'),
        'square_projection': projections, 'next': suggestion, 'bed_line': BED_LINE}
    expected = ', '.join(str(o['expected'])+' '+clean(o['product_name'],24) for o in summary['expected_outputs']) or summary['recipe_state']
    projection = 'Square → '+str(projections[0]['quantity'])+' if all good' if projections and projections[0]['quantity'] is not None else 'Square count unknown'
    # The structured summary carries full evidence; bounded channel text retains
    # the action link and mandatory bed statement even for long product names.
    next_text = (f"Next {suggestion['job_id']}" if suggestion and suggestion.get('job_id') else 'No queued fit')
    unavailable = summary['inspection_unavailable_reason']
    action = ('inspection unavailable: '+clean(unavailable,38)) if unavailable else 'Clear, count, bin, record'
    duration_text = f"{round(duration/60)}m" if duration is not None else '?m'
    prefix = f"{summary['printer'][:12]} #{job_id} {duration_text} · {expected[:24]}. Health: {health}. {projection}. {action}. {next_text}. "
    suffix = summary['inspection_url']+'. '+BED_LINE
    summary['text'] = prefix[:299-len(suffix)] + suffix
    return summary
