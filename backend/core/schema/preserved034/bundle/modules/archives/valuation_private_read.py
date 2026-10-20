"""Current-owner private historical unit inputs; caller owns rollback/lifetime."""
from fastapi import HTTPException
from core.financial_read_authority import verify_financial_reader
from modules.archives.valuation_parent_lock import lock_financial_attempt
from modules.archives.valuation_financial_graph import prove_financial_graph
from modules.archives.valuation_content_visibility import fence_content_membership,assert_content_visible
from modules.archives.valuation_payload import load_bounded_snapshot
from modules.archives.valuation_projection import parse_private_valuation,historical_extension_absent
from modules.archives.valuation_retained_evidence import verify_retained_basis


def read_private_valuation(db,request,actor,attempt_id):
    reader=verify_financial_reader(db,request,actor)
    fence_content_membership(db)
    try:
        lock_financial_attempt(db,attempt_id)
        graph=prove_financial_graph(db,attempt_id)
        platform=reader.role=='admin' and reader.group_id is None
        if not platform and (graph['owner_scope']!='org' or reader.group_id!=graph['owner_key']):
            raise ValueError('Different financial owner')
        assert_content_visible(db,graph=graph,principal=actor)
        payload=load_bounded_snapshot(db,attempt_id=attempt_id,birth_nonce=graph['attempt_birth'],identity_uid=graph['attempt_uid'])
        if payload is None:
            return {'state':'unavailable','reason':'missing_historical_valuation'}
        if historical_extension_absent(payload):
            return {'state':'unavailable','reason':'missing_historical_valuation'}
        unit=parse_private_valuation(payload)
        target=unit['target']
        if target is None or (unit['attempt_uid'],unit['attempt_birth'])!=(graph['attempt_uid'],graph['attempt_birth']):
            raise ValueError('Snapshot attempt differs')
        if any(target[key]!=graph[key] for key in ('owner_scope','owner_key','job_uid','item_uid','order_uid')):
            raise ValueError('Snapshot owner differs')
        verify_retained_basis(db,unit)
        # No identifiers from unproved or foreign material are projected.
        sources=[]
        for source in unit['sources']:
            projected=dict(source)
            if 'evidence_nonce' not in source:
                projected['spool_uid']=None;projected['birth_nonce']=None
            sources.append(projected)
        return dict(unit,sources=sources)
    except ValueError:
        raise HTTPException(404,'Attempt valuation not found',headers={'Cache-Control':'private, no-store'}) from None
