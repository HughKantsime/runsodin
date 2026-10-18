"""Private prospective valuation inputs; no amount, commit or public projection."""
from dataclasses import dataclass
from sqlalchemy import text
from core.dispatch_observation_executor import TransactionView
from core.economic_source_creation import SourceCreationError


_SEAL = object()
@dataclass(frozen=True, eq=False)
class _MaterialFence:
    executor: object
    scope: object
    plan: object
    context: object
    seal: object


def fence_material_membership(executor, scope, plan, attempt_id):
    from core.attempt_start_plan import _fresh_capture_context
    context = _fresh_capture_context(executor, scope, plan, attempt_id)
    view = TransactionView(executor)
    if view.dialect.name == 'postgresql':
        view.execute(text('LOCK TABLE public.filament_slots, public.print_files, public.spools IN SHARE MODE NOWAIT'))
    else:
        view.execute(text('UPDATE spools SET id=id WHERE 0'))
    scope.checkpoint(executor)
    return _MaterialFence(executor, scope, plan, context, _SEAL)


def _target(view, context):
    if context.identity_uid is None or context.job_id is None:
        return None, 'missing_commerce_binding'
    row = view.execute(text('''SELECT p.economic_uid AS attempt_uid,p.economic_birth_nonce AS birth,
        j.id AS job_id,j.economic_uid AS job_uid,j.charged_to_org_id,j.order_item_id,
        i.economic_uid AS item_uid,i.order_id,o.economic_uid AS order_uid,o.org_id,
        o.economic_global_origin_nonce AS origin_nonce
        FROM print_jobs p JOIN jobs j ON j.id=p.scheduled_job_id
        JOIN order_items i ON i.id=j.order_item_id JOIN orders o ON o.id=i.order_id
        WHERE p.id=:id'''), {'id': context.id}).mappings().first()
    if row is None or (row['attempt_uid'], row['birth'], row['job_id']) != (context.identity_uid, context.birth_nonce, context.job_id):
        raise SourceCreationError('Fresh valuation target changed')
    if row['charged_to_org_id'] != row['org_id']:
        raise SourceCreationError('Valuation target owner differs')
    owner = ('global', 0) if row['org_id'] is None else ('org', row['org_id'])
    if owner[0] == 'org' and (type(owner[1]) is not int or owner[1] <= 0):
        raise SourceCreationError('Invalid valuation owner')
    for kind, identifier, uid in (('attempt', context.id, row['attempt_uid']), ('job', row['job_id'], row['job_uid']), ('item', row['order_item_id'], row['item_uid']), ('order', row['order_id'], row['order_uid'])):
        registry = view.execute(text('SELECT resource_kind,resource_id,owner_scope,owner_key FROM cost_identities WHERE identity_uid=:uid'), {'uid': uid}).first()
        if registry is None or tuple(registry) != (kind, identifier, *owner):
            raise SourceCreationError('Valuation registry differs')
    lifetime = view.execute(text('SELECT resource_kind,resource_id,state,identity_uid,owner_scope,owner_key FROM cost_source_lifetimes WHERE birth_nonce=:birth'), {'birth': context.birth_nonce}).first()
    if lifetime is None or tuple(lifetime) != ('attempt', context.id, 'known', context.identity_uid, *owner):
        raise SourceCreationError('Valuation attempt lifetime differs')
    binding = view.execute(text('SELECT order_uid,item_uid,owner_scope,owner_key FROM cost_job_bindings WHERE job_uid=:uid'), {'uid': row['job_uid']}).first()
    if binding is None or tuple(binding) != (row['order_uid'], row['item_uid'], *owner):
        raise SourceCreationError('Valuation commerce binding differs')
    if owner[0] == 'global':
        origin = view.execute(text('SELECT origin_nonce,proof_kind,owner_scope,owner_key FROM cost_global_order_origins WHERE order_uid=:uid'), {'uid': row['order_uid']}).first()
        excluded = view.execute(text('SELECT 1 FROM cost_global_origin_exclusions WHERE order_uid=:uid'), {'uid': row['order_uid']}).first()
        if excluded is not None or origin is None or tuple(origin) != (row['origin_nonce'], 'verified_platform_admin_global_creation', 'global', 0):
            return None, 'unattested_company_origin'
    return {'owner_scope': owner[0], 'owner_key': owner[1], 'currency': None, 'job_uid': row['job_uid'], 'item_uid': row['item_uid'], 'order_uid': row['order_uid']}, None


def freeze_unit_valuation(executor, scope, plan, attempt_id, material_plan, material_fence):
    """Caller must fence mapping membership before deriving material_plan."""
    from core.attempt_start_plan import _fresh_capture_context
    context = _fresh_capture_context(executor, scope, plan, attempt_id)
    if type(material_fence) is not _MaterialFence or material_fence.seal is not _SEAL or material_fence.executor is not executor or material_fence.scope is not scope or material_fence.plan is not plan or material_fence.context != context:
        raise SourceCreationError('Material capture fence differs')
    view = TransactionView(executor)
    target, reason = _target(view, context)
    clock_sql = "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')" if view.dialect.name == 'postgresql' else "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    clock = view.execute(text(clock_sql)).scalar_one()
    if not isinstance(clock, str) or not clock.endswith('Z'):
        raise SourceCreationError('Unable to prove UTC valuation capture time')
    result = {'version': 2, 'state': 'unknown', 'reason': reason,
              'attempt_uid': context.identity_uid, 'attempt_birth': context.birth_nonce,
              'captured_at': str(clock), 'target': target, 'sources': []}
    if target is None:
        return result
    if not isinstance(material_plan, dict) or material_plan.get('state') != 'complete':
        result['reason'] = 'unresolved_material_mapping'; return result
    allocations = material_plan.get('allocations')
    if not isinstance(allocations, list) or not 1 <= len(allocations) <= 16 or any(not isinstance(x, dict) or type(x.get('spool_id')) is not int or x['spool_id'] <= 0 for x in allocations):
        result['reason'] = 'invalid_bounded_sources'; return result
    identifiers = sorted({x['spool_id'] for x in allocations})
    for identifier in identifiers:
        statement = 'SELECT id FROM spools WHERE id=:id FOR UPDATE NOWAIT' if view.dialect.name == 'postgresql' else 'UPDATE spools SET id=id WHERE id=:id RETURNING id'
        if view.execute(text(statement), {'id': identifier}).first() is None:
            raise SourceCreationError('Selected valuation source disappeared')
    # This fence also stabilizes missing configuration, without changing its writer.
    if view.dialect.name == 'postgresql':
        view.execute(text('LOCK TABLE public.finance_currency_settings IN SHARE MODE NOWAIT'))
    else:
        view.execute(text('UPDATE finance_currency_settings SET currency=currency WHERE 0'))
    config = view.execute(text('SELECT currency FROM finance_currency_settings WHERE owner_scope=:scope AND owner_key=:key' + (' FOR SHARE NOWAIT' if view.dialect.name == 'postgresql' else '')), {'scope': target['owner_scope'], 'key': target['owner_key']}).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- only a constant dialect lock suffix is appended; values are bound
    target_currency = config[0] if config is not None else None
    target['currency'] = target_currency
    for identifier in identifiers:
        row = view.execute(text('''SELECT s.id,s.org_id,s.economic_uid,s.economic_birth_nonce,s.purchase_evidence_nonce,s.purchase_revision,
            l.state,l.resource_kind,l.resource_id,l.identity_uid,l.owner_scope,l.owner_key,
            r.resource_kind AS registry_kind,r.resource_id AS registry_id,r.owner_scope AS registry_scope,r.owner_key AS registry_key
            FROM spools s LEFT JOIN cost_source_lifetimes l ON l.birth_nonce=s.economic_birth_nonce
            LEFT JOIN cost_identities r ON r.identity_uid=s.economic_uid WHERE s.id=:id'''), {'id': identifier}).mappings().first()
        from core.spool_purchase_evidence import _known
        source = {'spool_uid': row['economic_uid'], 'birth_nonce': row['economic_birth_nonce'], 'state': 'unknown', 'reason': 'unproved_source_lifetime'}
        if _known(row):
            if (row['owner_scope'], row['owner_key']) != (target['owner_scope'], target['owner_key']):
                source['reason'] = 'foreign_source_owner'
            elif row['purchase_evidence_nonce'] is None:
                source['reason'] = 'missing_purchase_evidence'
            else:
                receipt = view.execute(text('SELECT evidence_nonce,spool_uid,birth_nonce,revision,owner_scope,owner_key,currency,price_decimal,net_weight_g_decimal,weight_basis,provenance FROM spool_purchase_evidence WHERE evidence_nonce=:nonce'), {'nonce': row['purchase_evidence_nonce']}).mappings().first()
                if receipt is None or tuple(receipt[k] for k in ('spool_uid','birth_nonce','revision','owner_scope','owner_key')) != (row['economic_uid'], row['economic_birth_nonce'], row['purchase_revision'], target['owner_scope'], target['owner_key']):
                    raise SourceCreationError('Valuation purchase receipt differs')
                source.update({k: receipt[k] for k in ('evidence_nonce','revision','currency','price_decimal','net_weight_g_decimal','weight_basis','provenance')})
                source['reason'] = 'missing_target_currency' if target_currency is None else ('currency_mismatch' if target_currency != receipt['currency'] else None)
                source['state'] = 'known' if source['reason'] is None else 'unknown'
        result['sources'].append(source)
    result['state'] = 'known' if all(x['state'] == 'known' for x in result['sources']) else 'unknown'
    result['reason'] = None if result['state'] == 'known' else 'unresolved_source_valuation'
    scope.checkpoint(executor)
    return result
