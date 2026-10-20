import json
import pytest
import requests
from fastapi import HTTPException
from modules.orders.square_client import SquareReader, tracked, present


def test_reader_has_no_mutation_operations():
    reader=SquareReader('sandbox','synthetic')
    for method,path in [('POST','/v2/inventory/changes/batch-create'),('PUT','/v2/catalog/object/V1'),('POST','/v2/payments'),('GET','https://evil.test')]:
        with pytest.raises(HTTPException) as error:reader._read(method,path)
        assert error.value.status_code==400


@pytest.mark.parametrize('payload,state,quantity',[
 ({},'unknown',None),({'counts':[]},'unknown',None),
 ({'counts':[{'catalog_object_id':'OTHER','location_id':'L1','state':'IN_STOCK','quantity':'900'}]},'unknown',None),
 ({'counts':[{'catalog_object_id':'V1','location_id':'L1','state':'IN_STOCK','quantity':'0'}]},'known','0'),
 ({'counts':[{'catalog_object_id':'V1','location_id':'L1','state':'IN_STOCK','quantity':'-2.00000'}]},'known','-2.00000')])
def test_exact_count_missing_is_unknown(monkeypatch,payload,state,quantity):
    monkeypatch.setattr(SquareReader,'_read',lambda *a,**kw:payload)
    result=SquareReader('sandbox','synthetic').count('V1','L1')
    assert result['state']==state and result['quantity']==quantity


@pytest.mark.parametrize('payload',[
 {'cursor':'next'}, {'counts':None},
 {'counts':[{'catalog_object_id':'V1','location_id':'L1','state':'IN_STOCK','quantity':'NaN'}]},
 {'counts':[{'catalog_object_id':'V1','location_id':'L1','state':'IN_STOCK','quantity':'1'}]*2}])
def test_incomplete_or_invalid_count_is_refused(monkeypatch,payload):
    monkeypatch.setattr(SquareReader,'_read',lambda *a,**kw:payload)
    with pytest.raises(HTTPException):SquareReader('sandbox','synthetic').count('V1','L1')


def test_location_tracking_and_availability_override_global():
    assert not tracked({'track_inventory':True,'location_overrides':[{'location_id':'L1','track_inventory':False}]},'L1')
    assert tracked({'location_overrides':[{'location_id':'L1','track_inventory':True}]},'L1')
    assert not tracked({},'L1')
    assert not present({'present_at_all_locations':True,'absent_at_location_ids':['L1']},'L1')
    assert not present({'present_at_all_locations':True,'is_deleted':True},'L1')


@pytest.mark.parametrize('status,payload,code',[(401,{},502),(403,{},502),(429,{},503),(302,{},502),(500,{},502),(200,{'errors':[{'detail':'secret-sentinel'}]},502),(200,[],502)])
def test_transport_fixed_host_sanitized_errors_and_no_redirect(monkeypatch,status,payload,code):
    seen=[]
    class Response:
        status_code=status
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def iter_content(self,*a):yield json.dumps(payload).encode()
    class Session:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def request(self,*args,**kwargs):seen.append((args,kwargs,self.trust_env));return Response()
    monkeypatch.setattr(requests,'Session',Session)
    with pytest.raises(HTTPException) as error:SquareReader('sandbox','synthetic').merchant()
    assert error.value.status_code==code and 'secret-sentinel' not in str(error.value.detail)
    args,kwargs,trust=seen[0];assert args[:2]==('GET','https://connect.squareupsandbox.com/v2/merchants/me')
    assert kwargs['allow_redirects'] is False and kwargs['stream'] is True and trust is False


def test_network_error_does_not_echo_exception(monkeypatch):
    class Session:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def request(self,*a,**kw):raise requests.Timeout('private-sentinel')
    monkeypatch.setattr(requests,'Session',Session)
    with pytest.raises(HTTPException) as error:SquareReader('production','synthetic').merchant()
    assert 'private-sentinel' not in str(error.value.detail)
