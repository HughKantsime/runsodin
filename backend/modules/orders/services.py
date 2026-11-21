"""Cross-module order lifecycle services; caller owns authorization and transaction."""


def lock_tenant_for_deletion(db, owner):
    """Serialize organization removal against connector and production writers."""
    from modules.orders.square_store import lock_owner
    lock_owner(db, owner)


def forget_tenant(db, owner):
    """Refuse retained production history, then remove local connector state.

    Caller must hold lock_tenant_for_deletion in this same transaction. This
    performs no commit and no external requests.
    """
    from modules.orders.square_store import forget_tenant as forget
    forget(db, owner)


def retain_component_model(db,owner,model_id):
    from modules.orders.finishing import guard_model_delete
    guard_model_delete(db,owner,model_id)


def assert_job_history_deletable(db, job_id):
    """Keep the job and its history while its production acceptance is retained."""
    from sqlalchemy import text
    from fastapi import HTTPException
    db.execute(text("UPDATE jobs SET id=id WHERE id=:id"), {"id":job_id})
    if db.execute(text('SELECT 1 FROM production_acceptances a JOIN jobs j ON j.economic_uid=a.job_uid WHERE j.id=:id LIMIT 1'),{'id':job_id}).first():
        raise HTTPException(409, 'Job has a retained production inspection; retain its timeline with that business record')


def print_next_count(db, user):
    """Products that need a plate, from recorded stock only: database reads, no Square call.

    None when the caller has no production organization or the feature is not licensed.
    """
    from fastapi import HTTPException
    from license_manager import get_license
    if not get_license().has_feature('products'):
        return None
    from modules.orders.replenishment import print_next_count as count
    try:
        return count(db, user)
    except HTTPException:
        return None


def inspection_audit_counters(db, logs):
    """Resolve retained counter names for this local, visibility-filtered audit page."""
    import json
    from sqlalchemy import select
    from core.inspection_details import local_count_details
    from core.log_redaction import redact_text
    from modules.orders import batch_production, finishing

    batch_ids = {}
    finishing_ids = {}
    for entry, *_ in logs:
        details = entry.details
        if entry.entity_type != 'production_acceptance' or not isinstance(details, dict):
            continue
        owner, rid = details.get('counted_by_receipt_owner'), details.get('receipt_id')
        if type(owner) is not int or not isinstance(rid, str):
            continue
        if entry.action == 'production.inspected':
            batch_ids[rid] = owner
        elif entry.action in ('production.component_inspected', 'production.assembly_finished'):
            finishing_ids.setdefault(owner, set()).add(rid)

    records = {}
    if batch_ids:
        receipts = batch_production.all_receipts.c
        for rid, owner, definition in db.execute(select(receipts.id, receipts.owner, receipts.definition)
                                                .where(receipts.id.in_(batch_ids))):
            if batch_ids[rid] == owner:
                records[(owner, rid)] = json.loads(definition)
    for owner, ids in finishing_ids.items():
        doc, _ = finishing.load(db, owner)
        for group in ('components', 'finishes'):
            for row in doc[group].values():
                if row['id'] in ids:
                    records[(owner, row['id'])] = row
    return {key: redact_text(local_count_details(row)['counted_by']) for key, row in records.items()}
