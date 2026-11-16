"""Read evidence for one visible archive; never infer a spool from today's slot."""
import json
from sqlalchemy import text
from core import filament_ledger as ledger
from core.rbac import check_org_access
from core.tenancy_mode import install_has_organizations
from core.print_attempts import sqlalchemy_runner, moment
from .accounting_causes import describe


def decoded(value, fallback):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def physical_print(db, archive):
    return db.execute(text('SELECT * FROM print_jobs WHERE id=:id'),
                      {'id': archive['print_job_id']}).mappings().first()


def not_settleable(db, archive, physical):
    if archive.get('ownership_source') == 'project_import' or archive.get('accounting_reason') == 'project_import' or not archive.get('print_job_id'):
        return 'Imported history has no print to settle.'
    snapshot = decoded(physical['spool_snapshot'], {}) if physical else {}
    if (archive.get('education_submission_id') is not None
            or snapshot.get('content_owner', {}).get('education_submission_id') is not None):
        return 'This print belongs to an Education submission. Its filament is not settled here.'
    if physical and (archive.get('ownership_state') == 'known' or not install_has_organizations(db)):
        return None
    return "This print's job, file or organisation records disagree. An administrator must review it before its filament can be settled."


def readable_spool(db, spool_id, user, org_id=None, *, for_write=False):
    row = db.execute(text('SELECT * FROM spools WHERE id=:id'), {'id': spool_id}).mappings().first()
    if not row or not check_org_access(user, row['org_id']):
        return None
    if for_write and row['org_id'] is not None and row['org_id'] != org_id:
        return None
    return row


def later_balances(db, archive, spool_ids, user):
    ended = moment(archive['completed_at'])
    if ended is None:
        return []
    result = []
    for spool_id in sorted(set(spool_ids)):
        spool = readable_spool(db, spool_id, user)
        if spool is None:
            continue
        evidence = []
        weighed = moment(db.execute(text('SELECT value FROM system_config WHERE key=:key'),
                                    {'key': f'filament_balance_weighed:{spool_id}'}).scalar())
        created = moment(spool['created_at'])
        if weighed and weighed > ended and (created is None or weighed >= created):
            evidence.append((weighed, 'weighed'))
        measured = moment(spool['remaining_measured_at'])
        if measured and measured >= ended:
            evidence.append((measured, 'measured'))
        for row in db.execute(text("SELECT used_at,notes FROM spool_usage WHERE spool_id=:id AND job_id IS NULL"), {'id': spool_id}):
            at = moment(row[0])
            if row[1] in ('manual: weigh adjustment', 'Manual weigh adjustment') and at and at > ended:
                evidence.append((at, 'weighed'))
        if evidence:
            at, how = max(evidence)
            result.append(dict(spool_id=spool_id, at=at.isoformat(), how=how))
    return result


def preview(db, archive, user, entered_spool_ids=()):
    physical = physical_print(db, archive)
    refusal = not_settleable(db, archive, physical)
    job_id = physical['scheduled_job_id'] if physical else None
    run = sqlalchemy_runner(db)
    prior = ledger.prior_attempt_usage(run, job_id, archive['print_job_id']) if physical else None
    snapshot = decoded(physical['spool_snapshot'], {}) if physical else {}
    plan = snapshot.get('material_plan') or {}
    allocations = plan.get('allocations') or snapshot.get('slots') or []
    consumed = decoded(archive.get('consumption_json'), [])
    measured = physical['filament_used_g'] if physical else None
    suggested = []
    for item in allocations:
        spool = readable_spool(db, item.get('spool_id'), user)
        if spool is None:
            continue
        held = next((entry for entry in consumed if entry.get('slot_number') == item.get('slot_number')
                     and entry.get('grams_used') is not None), {})
        grams = measured if len(allocations) == 1 and measured is not None else held.get('grams_used', item.get('estimated_grams'))
        suggested.append(dict(slot_number=item.get('slot_number'), spool_id=spool['id'],
                              spool_label=f"Spool {spool['id']}" + (f" · {spool['color_name']}" if spool['color_name'] else ''), grams=grams,
                              remaining_g=spool['remaining_weight_g']))
    manual = ledger.manual_rows_for_print(run, job_id, archive['print_job_id']) if physical else []
    manual = [r for r in manual if readable_spool(db, r['spool_id'], user) is not None]
    return dict(archive_id=archive['id'], job_id=job_id, accounting_state=archive['accounting_state'],
                accounting_reason=archive['accounting_reason'],
                cause=describe('attempt_usage_without_archive' if prior else archive['accounting_reason'], archive.get('ownership_source')),
                settleable=refusal is None and archive['accounting_state'] == 'unresolved',
                not_settleable=refusal or ("This print's filament is already settled. Nothing was changed." if archive['accounting_state'] != 'unresolved' else None),
                grams=dict(measured=measured, sliced_estimate=plan.get('estimated_total')),
                suggested=suggested, manual_usage=manual, prior_usage=prior,
                balance_set_after_print=later_balances(db, archive,
                    [r['spool_id'] for r in suggested] + list(entered_spool_ids), user))


def annotate_settled(db, payload, user):
    entries = payload.get('consumption', [])
    settled = next((item for item in entries if item.get('settled_at')), None)
    payload['accounting_settled'] = None
    if settled:
        actor = db.execute(text('SELECT username,group_id FROM users WHERE id=:id'),
                           {'id': settled.get('settled_by_user_id')}).first()
        name = actor[0] if actor and check_org_access(user, actor[1]) else None
        payload['accounting_settled'] = dict(by=name, at=settled['settled_at'])
    return payload
