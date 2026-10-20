"""Explicit operations checks. No scheduler or provider mutations."""
from fastapi import APIRouter,Depends
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role,require_interactive_session
from modules.orders import operations as service
from modules.orders.routes.show_orders import PrivateRoute

class OperationsRoute(PrivateRoute):
    invalid_detail='Invalid operations details. Check dates, whole quantities and required fields.'
operator=require_role('operator',scope='read')
writer=require_role('operator',scope='write')
router=APIRouter(prefix='/products/production/operations',tags=['Operations preparation'],route_class=OperationsRoute,dependencies=[Depends(operator),Depends(require_interactive_session())])

@router.get('')
def read(user=Depends(operator),db:Session=Depends(get_db)):return service.read(db,user)
@router.put('/kit')
def kit(data:service.KitEdit,user=Depends(writer),db:Session=Depends(get_db)):return service.edit_kit(db,user,data)
@router.put('/events')
def event(data:service.EventEdit,user=Depends(writer),db:Session=Depends(get_db)):return service.edit_event(db,user,data)
@router.put('/events/{event_id}/packing/{item_id}')
def pack(event_id:str,item_id:str,data:service.PackEdit,user=Depends(writer),db:Session=Depends(get_db)):return service.pack(db,user,event_id,item_id,data)
@router.get('/conflicts')
def conflicts(user=Depends(operator),db:Session=Depends(get_db)):return service.conflicts(db,user)
@router.put('/capacity')
def capacity(data:service.CapacityEdit,user=Depends(writer),db:Session=Depends(get_db)):return service.set_capacity(db,user,data)
@router.get('/jobs')
def jobs(user=Depends(operator),db:Session=Depends(get_db)):return service.jobs(db,user)
@router.get('/jobs/{job_id}/readiness')
def readiness(job_id:int,user=Depends(operator),db:Session=Depends(get_db)):return service.readiness(db,user,job_id)
