"""Read evidence for one visible archive; never infer a spool from today's slot."""
import json
import math
from sqlalchemy import text
from core import filament_ledger as ledger
from core.rbac import check_org_access
from core.tenancy_mode import install_has_organizations
from core.print_attempts import sqlalchemy_runner, moment
from core.system_config_values import config_text
from .accounting_causes import describe


def decoded(value, fallback):
    try:
        result = json.loads(value) if isinstance(value, str) else value
        return result if isinstance(result, type(fallback)) else fallback
    except (TypeError, ValueError):
        return fallback


def physical_print(db, archive):
    row = db.execute(text('SELECT * FROM print_jobs WHERE id=:id'),
                      {'id': archive['print_job_id']}).mappings().first()
    if row and row['scheduled_job_id'] is not None and not db.execute(
            text('SELECT 1 FROM jobs WHERE id=:id'), {'id': row['scheduled_job_id']}).first():
        return dict(row, scheduled_job_id=None)
    return row


def not_settleable(db, archive, physical, has_org=None):
    if archive.get('ownership_source') == 'project_import' or archive.get('accounting_reason') == 'project_import':
        return 'Imported history has no print to settle.'
    snapshot = decoded(physical['spool_snapshot'], {}) if physical else {}
    owner = snapshot.get('content_owner')
    if (archive.get('education_submission_id') is not None
            or isinstance(owner, dict) and owner.get('education_submission_id') is not None):
        return 'This print belongs to an Education submission. Its filament is not settled here.'
    if physical and malformed_snapshot(snapshot):
        return "This print's start records cannot be read. An administrator must review them before its filament can be settled."
    if not physical:
        return 'This archive has no physical print record. An administrator must review it before its filament can be settled.'
    if archive.get('ownership_state') == 'known' or not (install_has_organizations(db) if has_org is None else has_org):
        return None
    return "This print's job, file or organisation records disagree. An administrator must review it before its filament can be settled."


def malformed_snapshot(snapshot):
    if 'content_owner' in snapshot and not isinstance(snapshot['content_owner'], dict):
        return True
    if 'material_plan' in snapshot and not isinstance(snapshot['material_plan'], dict):
        return True
    for items in (snapshot.get('slots', []), snapshot.get('material_plan', {}).get('allocations', [])):
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            return True
    return False


HIDDEN_USAGE = "Part of this print's filament is on a spool you cannot see. Someone with access to it must settle this print."


def accounting_status(db, archive, physical=None, prior=None, has_org=None):
    physical = physical if physical is not None or has_org is not None else physical_print(db, archive)
    refusal = not_settleable(db, archive, physical, has_org)
    state = archive.get('accounting_state')
    if refusal:
        cause = dict(code='not_settleable', title=refusal, next_step='')
    else:
        cause = describe('attempt_usage_without_archive' if prior else archive.get('accounting_reason'), archive.get('ownership_source'))
    return dict(settleable=refusal is None and state == 'unresolved',
                not_settleable=refusal, accounting_cause=cause,
                accounting_badge=('Needs its filament settled' if refusal is None and state == 'unresolved' else
                                  'Records disagree' if state in ('unresolved', 'reconciliation_required') and refusal and ('records' in refusal or 'record.' in refusal) else None))


def archive_statuses(db, archives):
    """Use one batch of ledger evidence for list, comparison and summary causes."""
    from core.filament_read_evidence import batch_runner
    archives = list(archives)
    run = batch_runner(db, {a['print_job_id'] for a in archives if a.get('print_job_id')})
    evidence = getattr(run, 'filament_evidence', None)
    has_org = install_has_organizations(db)
    result = {}
    for archive in archives:
        physical = evidence.prints.get(archive['print_job_id']) if evidence else None
        job_id = physical['scheduled_job_id'] if physical and physical['scheduled_job_id'] in evidence.jobs else None
        prior = ledger.prior_attempt_usage(run, job_id, physical['id']) if physical else None
        result[archive['id']] = accounting_status(db, archive, physical, prior, has_org)
    return result


def readable_spool(db, spool_id, user, org_id=None, *, for_write=False):
    row = db.execute(text('SELECT * FROM spools WHERE id=:id'), {'id': spool_id}).mappings().first()
    if not row or not check_org_access(user, row['org_id']):
        return None
    if for_write and row['org_id'] is not None and row['org_id'] != org_id:
        return None
    return row


def later_balances(db, archive, spool_ids, user):
    ended = moment(archive['completed_at'])
    physical = physical_print(db, archive)
    started = moment(physical['started_at']) if physical else None
    result = []
    for spool_id in sorted(set(spool_ids)):
        spool = readable_spool(db, spool_id, user)
        if spool is None:
            continue
        if ended is None or started is None:
            result.append(dict(spool_id=spool_id, at=None, how='unknown_print_time'))
            continue
        evidence = []
        weigh_marker = db.execute(text('SELECT value FROM system_config WHERE key=:key'),
                                 {'key': f'filament_balance_weighed:{spool_id}'}).first()
        weighed = moment(config_text(weigh_marker[0])) if weigh_marker else None
        if weigh_marker and weighed is None:
            result.append(dict(spool_id=spool_id, at=None, how='unknown_balance_time'))
            continue
        created = moment(spool['created_at'])
        if created and created > ended:
            evidence.append((created, 'added'))
        if weighed and weighed >= started and (created is None or weighed >= created):
            evidence.append((weighed, 'weighed'))
        measured = moment(spool['remaining_measured_at'])
        if measured and measured >= started:
            evidence.append((measured, 'measured'))
        for row in db.execute(text("SELECT used_at,notes FROM spool_usage WHERE spool_id=:id AND job_id IS NULL"), {'id': spool_id}):
            at = moment(row[0])
            if row[1] in ('manual: weigh adjustment', 'Manual weigh adjustment') and at and at >= started:
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
    if malformed_snapshot(snapshot):
        snapshot = {}
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
        remaining = spool['remaining_weight_g']
        try:
            invalid = remaining is not None and not math.isfinite(float(remaining))
        except (TypeError, ValueError):
            invalid = True
        if invalid:
            refusal = f"Spool {spool['id']} has an invalid balance. Correct or weigh the spool, then try again."
            remaining = None
        suggested.append(dict(slot_number=item.get('slot_number'), spool_id=spool['id'],
                              spool_label=f"Spool {spool['id']}" + (f" · {spool['color_name']}" if spool['color_name'] else ''), grams=grams,
                              remaining_g=remaining))
    try:
        manual = ledger.manual_rows_for_print(run, job_id, archive['print_job_id']) if physical else []
        existing = ledger.existing_rows_for_print(run, job_id, archive['print_job_id']) if physical else []
    except ledger.InvalidSettlementEvidence as exc:
        refusal = str(exc)
        manual, existing = [], []
    if any(readable_spool(db, r['spool_id'], user) is None for r in manual + existing):
        refusal = HIDDEN_USAGE
        manual = []
    return dict(archive_id=archive['id'], job_id=job_id, accounting_state=archive['accounting_state'],
                accounting_reason=archive['accounting_reason'],
                cause=accounting_status(db, archive, physical, prior)['accounting_cause'],
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
        if settled.get('settled_auth_kind') == 'legacy_global_api_key':
            name = 'Global API key'
        payload['accounting_settled'] = dict(by=name, at=settled['settled_at'])
    for item in entries:
        item.pop('settled_by_user_id', None)
        item.pop('settled_by_token_id', None)
    payload.pop('consumption_json', None)
    return payload
