"""Atomic operator settlement. Only core.filament_ledger writes consumption."""
import json
import uuid
from datetime import datetime, timezone
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from typing import Literal
from sqlalchemy import text
from core import filament_ledger as ledger
from core.audit import log_audit
from core.interfaces.filament_ownership import ensure_local_filament_management
from core.order_production import prepare_print_job_write
from core.print_attempts import sqlalchemy_runner
from .access import assert_archive_access
from .settlement_data import physical_print, not_settleable, readable_spool, later_balances


class Entry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    spool_id: StrictInt = Field(gt=0)
    grams: float = Field(gt=0, le=10000, allow_inf_nan=False)


class Settlement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entries: list[Entry] = Field(default_factory=list, max_length=16)
    counted_usage_ids: list[StrictInt] = Field(default_factory=list)
    separate_usage_ids: list[StrictInt] = Field(default_factory=list)
    no_deduction: Literal['nothing_extruded', 'recorded_by_hand', 'spool_not_tracked', 'accept_existing', 'balance_already_updated'] | None = None
    note: str = Field(default='', max_length=200)
    deduct_despite_later_balance: list[StrictInt] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique(self):
        for values in ([entry.spool_id for entry in self.entries], self.counted_usage_ids,
                       self.separate_usage_ids, self.deduct_despite_later_balance):
            if len(values) != len(set(values)) or any(value <= 0 for value in values):
                raise ValueError('Spool and usage selections must be unique positive IDs')
        if set(self.counted_usage_ids) & set(self.separate_usage_ids):
            raise ValueError('Review each manual use once')
        return self


def refuse(code, message, **details):
    raise HTTPException(409, dict(code=code, message=message, **details))


def archive_row(db, identifier, user):
    assert_archive_access(db, identifier, user)
    return dict(db.execute(text('SELECT * FROM print_archives WHERE id=:id'), {'id': identifier}).mappings().one())


def settle(db, identifier, body, user):
    archive = archive_row(db, identifier, user)
    # Parent -> physical print is the existing terminal/reset lock protocol.
    # Then spools in ascending order -> archive -> claim keys, as tray confirmation.
    if archive['print_job_id']:
        prepare_print_job_write(db, archive['print_job_id'])
    physical = physical_print(db, archive)
    job_id = physical['scheduled_job_id'] if physical else None
    run = sqlalchemy_runner(db)
    manual = ledger.manual_rows_for_print(run, job_id, archive['print_job_id']) if physical else []
    existing = ledger.existing_rows_for_print(run, job_id, archive['print_job_id']) if physical else []
    lock_ids = {entry.spool_id for entry in body.entries} | {r['spool_id'] for r in manual + existing}
    # Tray confirmation may already hold one spool before discovering other
    # material sources. Do not wait while holding a different spool on PostgreSQL.
    postgres = db.get_bind().dialect.name == 'postgresql'
    for spool_id in sorted(lock_ids):
        db.execute(text('SELECT id FROM spools WHERE id=:id FOR UPDATE NOWAIT' if postgres
                        else 'UPDATE spools SET id=id WHERE id=:id'), {'id': spool_id})
    db.execute(text('SELECT id FROM print_archives WHERE id=:id FOR UPDATE NOWAIT' if postgres
                    else 'UPDATE print_archives SET id=id WHERE id=:id'), {'id': identifier})
    archive = archive_row(db, identifier, user)
    if archive['accounting_state'] != 'unresolved':
        refuse('not_unresolved', "This print's filament is already settled. Nothing was changed.")
    ensure_local_filament_management(archive['printer_id'])
    physical = physical_print(db, archive)
    unavailable = not_settleable(db, archive, physical)
    if unavailable:
        refuse('not_settleable', unavailable)
    job_id = physical['scheduled_job_id']
    prior = ledger.prior_attempt_usage(run, job_id, archive['print_job_id'])
    existing = ledger.existing_rows_for_print(run, job_id, archive['print_job_id']) if prior else []
    if prior and (body.no_deduction != 'accept_existing' or body.entries or body.counted_usage_ids):
        visible = [r for r in existing if readable_spool(db, r['spool_id'], user) is not None]
        evidence = f" ({sum(r['grams'] for r in visible):g} g, {visible[-1]['used_at']})" if visible else ''
        refuse('already_deducted', f"Filament for this print was already deducted{evidence}. Choose 'Keep the existing deduction' to close it.")
    if not prior and body.no_deduction == 'accept_existing':
        raise HTTPException(422, 'No existing deduction was found. Name a spool or choose a reason for no deduction.')
    manual = ledger.manual_rows_for_print(run, job_id, archive['print_job_id'])
    manual = [r for r in manual if readable_spool(db, r['spool_id'], user) is not None]
    if {r['usage_id'] for r in manual} != set(body.counted_usage_ids + body.separate_usage_ids):
        refuse('manual_usage_unreviewed', "Manual use was recorded against this job. Say for each row whether it was this print's filament.", manual_usage=manual)
    if (body.no_deduction and body.entries) or (not body.no_deduction and not body.entries):
        raise HTTPException(422, 'Name at least one spool, or choose a reason for no deduction')
    if not set(body.deduct_despite_later_balance) <= {entry.spool_id for entry in body.entries}:
        raise HTTPException(422, 'Only confirm balances for the spools used by this print')
    printer_org = db.execute(text('SELECT org_id FROM printers WHERE id=:id'), {'id': archive['printer_id']}).scalar()
    org_id = archive['org_id'] if archive['org_id'] is not None else printer_org
    spools = {}
    for entry in body.entries:
        spool = readable_spool(db, entry.spool_id, user, org_id, for_write=True)
        if spool is None:
            raise HTTPException(404, 'Spool not found')
        spools[entry.spool_id] = spool
    counted = [r for r in manual if r['usage_id'] in body.counted_usage_ids]
    if any(r['spool_id'] not in spools for r in counted):
        raise HTTPException(422, 'Name the spool and total grams for every manual use counted for this print')
    grams_by_spool = {}
    for entry in body.entries:
        grams = entry.grams - sum(r['grams'] for r in counted if r['spool_id'] == entry.spool_id)
        if grams < -0.05:
            refuse('manual_usage_exceeds', f"The manual use you counted already took more from spool {entry.spool_id} than this print used. Untick a row or correct the grams.")
        if grams > 0:
            grams_by_spool[entry.spool_id] = grams
    for later in later_balances(db, archive, grams_by_spool, user):
        spool_id = later['spool_id']
        if spool_id not in body.deduct_despite_later_balance:
            refuse('balance_set_after_print', f"Spool {spool_id} was weighed or measured after this print, so its balance already includes it. Choose 'No further deduction', or confirm you want to deduct anyway.")
    outcome = ledger.record_settlement(run, job_id, archive['print_job_id'], grams_by_spool=grams_by_spool,
                                       counted_usage_ids=body.counted_usage_ids, accept_existing=bool(prior), archive_id=identifier)
    if outcome.state == 'already_deducted':
        refuse('already_deducted', "Filament for this print was already deducted. Choose 'Keep the existing deduction' to close it.")
    actor = dict(settled_by_user_id=user.get('id'), settled_by_token_id=user.get('_token_id'),
                 settled_at=datetime.now(timezone.utc).isoformat(), counted_usage_ids=body.counted_usage_ids)
    deducted = {d['spool_id']: d for d in outcome.deductions}
    consumption = []
    if prior:
        totals = {}
        for row in existing:
            totals[row['spool_id']] = totals.get(row['spool_id'], 0) + row['grams']
        consumption = [dict(spool_id=spool, grams_used=grams, deducted_now_g=0,
                            quantity_provenance='operator_settled', **actor) for spool, grams in totals.items()]
    elif body.entries:
        for entry in body.entries:
            deduction = deducted.get(entry.spool_id, {})
            consumption.append(dict(spool_id=entry.spool_id, grams_used=entry.grams, deducted_now_g=deduction.get('grams', 0),
                quantity_provenance='operator_settled', inventory_shortfall_g=deduction.get('shortfall_g', 0),
                remaining_unknown=spools[entry.spool_id]['remaining_weight_g'] is None, **actor))
    if not consumption:
        consumption = [dict(spool_id=None, grams_used=None, deducted_now_g=0, reason=body.no_deduction, **actor)]
    state, reason = 'accounted', ('operator_accepted_existing' if prior else
                                  'operator_settled_no_deduction' if body.no_deduction else 'operator_settled')
    if any(d['shortfall_g'] > 0 for d in outcome.deductions):
        state, reason = 'reconciliation_required', 'inventory_shortfall'
    db.execute(text('UPDATE print_archives SET accounting_state=:state,accounting_reason=:reason,consumption_json=:consumption WHERE id=:id'),
               dict(state=state, reason=reason, consumption=json.dumps(consumption, allow_nan=False), id=identifier))
    for deduction in outcome.deductions:
        if deduction['spoolman_spool_id'] is not None:
            db.execute(text("INSERT INTO filament_delivery_outbox(id,print_job_id,spool_id,spoolman_spool_id,grams_used,state) "
                            "VALUES(:id,:print,:spool,:remote,:grams,'pending')"),
                       dict(id=uuid.uuid4().hex, print=archive['print_job_id'], spool=deduction['spool_id'],
                            remote=deduction['spoolman_spool_id'], grams=deduction['grams']))
    log_audit(db, 'archive.filament_settled', 'print_archive', identifier,
              dict(request=body.model_dump(), deductions=outcome.deductions, counted_usage_ids=body.counted_usage_ids,
                   separate_usage_ids=body.separate_usage_ids, previous_reason=archive['accounting_reason'], actor_token_id=user.get('_token_id')))
    return dict(archive_id=identifier, accounting_state=state, accounting_reason=reason,
                deductions=[{k: v for k, v in dict(d, remaining_unknown=d['remaining_g'] is None).items()
                             if k != 'spoolman_spool_id'} for d in outcome.deductions], counted_usage_ids=body.counted_usage_ids)
