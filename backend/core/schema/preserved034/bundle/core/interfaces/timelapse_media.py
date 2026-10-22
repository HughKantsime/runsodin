"""Opaque media operation boundary; no implementation, authority or I/O fallback."""
from typing import Any,Protocol,runtime_checkable
from fastapi import HTTPException
from core.registry import registry

MEDIA_METHODS=('capture_action_credential','admitted_response','sealed_lifetime_receipt',
               'authorize_retirement','prepare_request_lifetime_retirement','retire_sealed_lifetime','execute_edit')

@runtime_checkable
class TimelapseMediaProvider(Protocol):
    def capture_action_credential(self,request:Any,db:Any,credential:str,*,kind:str,carrier:str,purpose:str,selected:dict)->Any: ...
    def admitted_response(self,engine:Any,root:Any,tid:int,evidence:Any,*,range_header=None,head=False,filename=None,expected_nonce=None)->Any: ...
    def sealed_lifetime_receipt(self,engine:Any,evidence:Any,*,operation_id:str,original_id=None)->Any: ...
    def authorize_retirement(self,admission_session:Any,evidence:Any,tid:int,nonce:str)->None: ...
    def prepare_request_lifetime_retirement(self,engine:Any,root:Any,tid:int,evidence:Any,*,expected_nonce:str,budget:Any)->Any: ...
    def retire_sealed_lifetime(self,engine:Any,root:Any,tid:int,evidence:Any,*,expected_nonce:str,budget:Any)->Any: ...
    def execute_edit(self,engine:Any,root:Any,tid:int,evidence:Any,identifier:str,*,nonce=None,recipe=None,lookup=False)->Any: ...


def require_timelapse_media_provider()->TimelapseMediaProvider:
    provider=registry.get_provider('TimelapseMediaProvider')
    if not isinstance(provider,TimelapseMediaProvider) or any(not callable(getattr(provider,name,None)) for name in MEDIA_METHODS):
        raise HTTPException(503,'Timelapse media service unavailable',headers={'Cache-Control':'private, no-store'})
    return provider
