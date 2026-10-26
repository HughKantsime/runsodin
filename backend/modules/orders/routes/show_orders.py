"""Private guided intake. No external payment/shipping/customer-message actions."""
from fastapi import APIRouter,Depends,HTTPException,Query
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role,require_action,require_interactive_session
from modules.orders import show_fulfillment as service
from modules.orders.show_order_schemas import Intake,Handoff

from core.private_routes import PRIVATE, PrivateRoute

operator=require_role('operator',scope='read')
interactive=Depends(require_interactive_session())
router=APIRouter(prefix='/orders',tags=['Show order fulfillment'],route_class=PrivateRoute,dependencies=[Depends(operator),interactive])

@router.get('/show-commitments')
def commitments(offset:int=Query(0,ge=0,le=1000000),user=Depends(operator),db:Session=Depends(get_db)):
    return service.overview(db,user,offset)

@router.get('/{order_id}/show-fulfillment')
def read(order_id:int,user=Depends(operator),db:Session=Depends(get_db)):
    return service.read(db,user,order_id)

@router.put('/{order_id}/show-fulfillment')
def save(order_id:int,data:Intake,user=Depends(require_action('orders.edit',scope='write')),db:Session=Depends(get_db)):
    return service.intake(db,user,order_id,data)

@router.post('/{order_id}/show-fulfillment/dispatch')
def dispatch(order_id:int,data:Handoff,user=Depends(require_action('orders.ship',scope='write')),db:Session=Depends(get_db)):
    return service.dispatch(db,user,order_id,data)


from modules.orders.show_order_schemas import ShippingEdit

@router.put('/{order_id}/show-fulfillment/shipping')
def shipping(order_id:int,data:ShippingEdit,user=Depends(require_action('orders.edit',scope='write')),db:Session=Depends(get_db)):
    return service.save_shipping(db,user,order_id,data)
