"""Queue suggested plates from the print-next list as held jobs.

Jobs are staged through the shared queue-creation boundary
(core.interfaces.queue_creation), the same one archive reprints and file
scheduling use, with the recipe's exact file, plate, payload hash and printer.
Never contacts Square beyond the list's own read-only count read, never starts
a printer. Each item is re-checked against the current list under the owner
lock, so a repeated request finds its plates already on the way and adds none.
"""
import os
from typing import Annotated

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy import text, update

from core.models import SystemConfig
from modules.orders import assortment, replenishment, replenishment_evidence as evidence, square_store as store

MAX_PLATES = 50


class QueueItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    product_id: StrictInt = Field(gt=0)
    recipe_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    plates: Annotated[StrictInt, Field(ge=1, le=MAX_PLATES)]


class QueueRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[QueueItem] = Field(min_length=1, max_length=100)
    # Held by default: an operator releases each job, or asks for it ready here.
    ready: StrictBool = False


def plate_source(db, owner, user, recipe, hashes):
    """The recipe's printer and unchanged print file, or a plain reason it cannot be queued."""
    payload = recipe['payload']
    from core.interfaces.education_policy import assert_generic_model_reference_allowed
    assert_generic_model_reference_allowed(db, payload['model_id'])
    scope = '((:owner=0 AND org_id IS NULL) OR (:owner>0 AND org_id=:owner))'
    printer = db.execute(text('SELECT id,api_type FROM printers WHERE id=:id AND ' + scope),
                         {'id': payload['printer_id'], 'owner': owner}).mappings().first()
    if printer is None or not db.execute(text('SELECT 1 FROM models WHERE id=:id AND ' + scope),
                                         {'id': payload['model_id'], 'owner': owner}).first():
        return None, 'The recipe\'s printer or model is no longer in this organization. Qualify a new recipe.'
    file = db.execute(text('SELECT id,model_id,stored_path FROM print_files WHERE id=:id AND ' + scope),
                      {'id': payload['print_file_id'], 'owner': owner}).mappings().first()
    if file is None or file['model_id'] != payload['model_id'] or not file['stored_path'] or not os.path.isfile(file['stored_path']):
        return None, 'The recipe\'s print file is no longer available. Qualify a new recipe.'
    if db.execute(text('SELECT 1 FROM education_submissions WHERE print_file_id=:id'), {'id': file['id']}).first():
        return None, 'Education files require their approval workflow.'
    if file['id'] not in hashes:
        from core.print_payload_snapshot import hash_regular_payload, PayloadUnavailable
        try:
            hashes[file['id']] = hash_regular_payload(file['stored_path'])
        except (PayloadUnavailable, OSError, TypeError):
            hashes[file['id']] = None
    if hashes[file['id']] != payload['payload_sha256']:
        return None, 'The recipe\'s print file changed since it was qualified. Qualify a new recipe.'
    from core.print_payload import validate_plate
    try:
        validate_plate(file['stored_path'], payload['plate_index'], printer['api_type'])
    except HTTPException as exc:
        return None, f'The recipe\'s plate cannot be printed as stored: {exc.detail}'
    return printer, None


def queue(db, user, data):
    # Queueing creates jobs: besides the route's own permission the caller needs jobs.create,
    # read from the same live matrix (the platform superadmin always passes).
    from core.rbac import _is_platform_superadmin
    from core.rbac_policy import action_roles
    if not _is_platform_superadmin(user) and user.get('role') not in action_roles(db, 'jobs.create'):
        raise HTTPException(403, 'Insufficient permissions')
    owner = store.tenant(user)
    # Stock knowledge first (it may read Square, read-only, outside any lock), then authority under locks.
    known = replenishment.stock(db, owner)
    if known['error']:
        known = {**known, 'counts': {}}
    store.lock_owner(db, owner, user)
    visible = replenishment.full_view(db, owner, user)
    if not visible:
        raise HTTPException(403, replenishment.HIDDEN_WORK)
    # One queue request at a time per owner, including the global owner 0.
    if db.execute(update(SystemConfig).where(SystemConfig.key == assortment.key(owner)).values(value=SystemConfig.value)
                  .execution_options(synchronize_session=False)).rowcount != 1:
        raise HTTPException(409, 'Set a reorder point for a product before queueing plates.')
    latest, _ = store.load(db, owner)
    if (latest['revision'] if latest else None) != known['revision']:
        raise HTTPException(409, 'Square connection changed. Reload the list before queueing.')
    from core.interfaces.queue_creation import require_queue_creation_provider
    from core.quota import charge_job_quota
    from core.audit import log_audit
    from modules.jobs.models import Job
    creator = require_queue_creation_provider()
    queued, skipped, hashes = [], [], {}
    seen = set()
    for item in data.items:
        if item.product_id in seen:
            raise HTTPException(422, 'List each product once.')
        seen.add(item.product_id)
        product = store.own_product(db, owner, item.product_id, lock=True)
        # Rebuilt for every item: plates staged for an earlier item (a mixed plate) already count as on the way.
        rows, _ = replenishment.build(db, owner, user, known, visible=visible)
        row = next((r for r in rows if r['product_id'] == item.product_id), None)
        if row is None or row['status'] == 'blocked':
            skipped.append({'product_id': item.product_id, 'reason': row['reason'] if row else 'This product is not in the active assortment.'})
            continue
        if row['plates_needed'] <= 0:
            skipped.append({'product_id': item.product_id, 'reason': 'Nothing more is needed: stock and work already on the way cover it.'})
            continue
        if row['recipe']['id'] != item.recipe_id:
            skipped.append({'product_id': item.product_id, 'reason': 'The suggested plate changed. Reload the list.'})
            continue
        try:
            recipe = evidence.load_recipe(db, owner, item.recipe_id)
        except evidence.ScanProblem as exc:
            raise HTTPException(409, str(exc)) from None
        printer, problem = plate_source(db, owner, user, recipe, hashes)
        if problem:
            skipped.append({'product_id': item.product_id, 'reason': problem})
            continue
        plates = min(item.plates, row['plates_needed'])
        charge_job_quota(db, user, plates)
        payload, jobs = recipe['payload'], []
        for _ in range(plates):
            job = Job(item_name=recipe['name'], model_id=payload['model_id'], print_file_id=payload['print_file_id'],
                      payload_sha256=payload['payload_sha256'], plate_index=payload['plate_index'], printer_id=printer['id'],
                      # The recipe is qualified on this printer and configuration: pin it, as a reprint does.
                      target_type='specific', target_filter=str(printer['id']), is_locked=False,
                      status='pending', hold=not data.ready, quantity=1, priority=3,
                      quantity_on_bed=row['recipe']['pieces_per_plate'], duration_hours=row['recipe']['hours_per_plate'],
                      submitted_by=user.get('id'), charged_to_user_id=user.get('id'), charged_to_org_id=owner or None,
                      notes=f'Stock replenishment for {product.name}')
            creator.stage_job(db, job, user)
            jobs.append(job)
        db.flush()
        ids = [job.id for job in jobs]
        log_audit(db, 'production.replenishment_queued', 'product', item.product_id,
                  {'recipe_id': item.recipe_id, 'plates': plates, 'job_ids': ids, 'held': not data.ready,
                   'on_hand': row['on_hand']['quantity'], 'stock_read_at': row['on_hand']['read_at'], 'operator_id': user.get('id')})
        queued.append({'product_id': item.product_id, 'recipe_id': item.recipe_id, 'plates': plates, 'job_ids': ids, 'held': not data.ready})
    db.commit()
    return {'queued': queued, 'skipped': skipped}
