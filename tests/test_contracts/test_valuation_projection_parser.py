"""Closed parser refuses malformed input; this supplies no read authorization."""
import json
from copy import deepcopy
from uuid import uuid4
import pytest
from modules.archives.valuation_projection import parse_private_valuation


def snapshot():
    return {'version':1, 'unit_valuation': {'version':2,'state':'known','reason':None,
        'attempt_uid':str(uuid4()),'attempt_birth':'a'*32,'captured_at':'2026-10-13T12:00:00.123Z',
        'target':{'owner_scope':'org','owner_key':1,'job_uid':str(uuid4()),'item_uid':str(uuid4()),'order_uid':str(uuid4()),'currency':'USD'},
        'sources':[{'spool_uid':str(uuid4()),'birth_nonce':'b'*32,'state':'known','reason':None,
            'evidence_nonce':'c'*32,'revision':1,'currency':'USD','price_decimal':'0','net_weight_g_decimal':'750.25',
            'weight_basis':'purchased_net_filament_g','provenance':'owner_attestation'}]}}


def test_genuine_uuid_and_nonce_grammars_preserve_exact_strings():
    value=snapshot()
    assert parse_private_valuation(json.dumps(value)) == value['unit_valuation']


@pytest.mark.parametrize('change', ['extra','bool_revision','wrong_uid','wrong_nonce','noncanonical','mismatch','foreign_basis','invalid_date','duplicate_sources','reason_object'])
def test_malformed_extension_refuses_wholesale(change):
    value=snapshot(); unit=value['unit_valuation']; source=unit['sources'][0]
    if change=='extra':unit['extra']='no'
    elif change=='bool_revision':source['revision']=True
    elif change=='wrong_uid':unit['attempt_uid']='a'*32
    elif change=='wrong_nonce':source['evidence_nonce']=str(uuid4())
    elif change=='noncanonical':source['price_decimal']='00.00'
    elif change=='mismatch':source['currency']='EUR'
    elif change=='foreign_basis':source.update(state='unknown',reason='foreign_source_owner');unit.update(state='unknown',reason='unresolved_source_valuation')
    elif change=='invalid_date':unit['captured_at']='2026-02-30T12:00:00.123Z'
    elif change=='duplicate_sources':unit['sources'].append(deepcopy(source))
    elif change=='reason_object':unit['reason']={}
    with pytest.raises(ValueError,match='Invalid private valuation'):parse_private_valuation(json.dumps(value))


@pytest.mark.parametrize('payload',[ '['*9+'0'+']'*9, '{"version":1,"version":1}', '{"version":1,"value":NaN}', '{"version":1,"padding":"'+'x'*65536+'"}'])
def test_budget_depth_duplicate_and_nonfinite_admission_refuse(payload):
    with pytest.raises(ValueError,match='Invalid private valuation'):parse_private_valuation(payload)
