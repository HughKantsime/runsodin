"""Verify parsed private basis against immutable receipts, never live spool prices."""
from sqlalchemy import text


def verify_retained_basis(db, unit):
    target = unit['target']
    if target is None:
        raise ValueError('Unproved valuation target')
    owner = (target['owner_scope'], target['owner_key'])
    for source in unit['sources']:
        if 'evidence_nonce' not in source:
            continue
        lifetime = db.execute(text('''SELECT resource_kind,resource_id,state,identity_uid,owner_scope,owner_key
            FROM cost_source_lifetimes WHERE birth_nonce=:birth'''), {'birth':source['birth_nonce']}).first()
        if lifetime is None or (lifetime[0], lifetime[2], lifetime[3], lifetime[4], lifetime[5]) != ('spool','known',source['spool_uid'],*owner):
            raise ValueError('Retained valuation lifetime differs')
        registry = db.execute(text('''SELECT resource_kind,resource_id,owner_scope,owner_key
            FROM cost_identities WHERE identity_uid=:uid'''), {'uid':source['spool_uid']}).first()
        if registry is None or tuple(registry) != ('spool',lifetime[1],*owner):
            raise ValueError('Retained valuation registry differs')
        columns = ('evidence_nonce','spool_uid','birth_nonce','revision','owner_scope','owner_key',
                   'currency','price_decimal','net_weight_g_decimal','weight_basis','provenance')
        row = db.execute(text('SELECT '+','.join(columns)+' FROM spool_purchase_evidence WHERE evidence_nonce=:nonce'),
                         {'nonce':source['evidence_nonce']}).first()
        expected = tuple(source[key] if key not in ('owner_scope','owner_key') else target[key] for key in columns)
        if row is None or tuple(row) != expected:
            raise ValueError('Retained valuation receipt differs')
