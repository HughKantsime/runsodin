"""Isolated private historical valuation audience."""
from fastapi import APIRouter,Depends,Request,Response,HTTPException
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role
from core.interfaces.attempt_valuation import require_attempt_valuation_provider
from modules.orders.valuation_schemas import PrivateUnitValuation,UnavailableValuation

router=APIRouter(prefix='/orders',tags=['Orders'])


@router.get('/attempts/{attempt_id}/unit-valuation',response_model=PrivateUnitValuation|UnavailableValuation,response_model_exclude_unset=True)
def attempt_unit_valuation(attempt_id:int,request:Request,response:Response,
                           actor:dict=Depends(require_role('viewer',scope='read')),
                           db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store'
    try:
        provider=require_attempt_valuation_provider()
        return provider.read_private_valuation(db,request,actor,attempt_id)
    except HTTPException as error:
        headers={key:value for key,value in (error.headers or {}).items() if key.lower()!='cache-control'}
        headers['Cache-Control']='private, no-store'
        raise HTTPException(error.status_code,error.detail,headers=headers) from None
