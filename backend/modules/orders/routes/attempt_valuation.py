"""Isolated private historical valuation audience."""
from fastapi import APIRouter,Depends,Request,Response,HTTPException
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role
from core.interfaces.attempt_valuation import require_attempt_valuation_provider
from modules.orders.valuation_schemas import PrivateUnitValuation,UnavailableValuation,OrderMeasuredCosts

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


@router.get('/{order_id}/measured-costs',response_model=OrderMeasuredCosts)
def order_measured_costs(order_id:int,request:Request,response:Response,
                         actor:dict=Depends(require_role('viewer',scope='read')),
                         db:Session=Depends(get_db)):
    response.headers['Cache-Control']='private, no-store'
    try:
        result=require_attempt_valuation_provider().read_order_measured_costs(db,request,actor,order_id)
        if result['filament_state']=='unknown':
            # Classification only, after financial authorization. The estimate
            # never fills a missing measured amount or changes existing labels.
            from modules.orders.models import Order,Product
            from modules.orders.product_costing import estimated_order_financials
            order=db.query(Order).filter(Order.id==order_id).one()
            estimated=estimated_order_financials(order,lambda identifier:db.query(Product).filter(Product.id==identifier).first())
            if estimated['estimated_cogs'] is not None:result['filament_state']='estimated_only'
        return result
    except HTTPException as error:
        headers={key:value for key,value in (error.headers or {}).items() if key.lower()!='cache-control'}
        headers['Cache-Control']='private, no-store'
        raise HTTPException(error.status_code,error.detail,headers=headers) from None
