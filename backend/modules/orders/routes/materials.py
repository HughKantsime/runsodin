"""Private explicit material custody, purchases and physical receipt facts."""
from fastapi import APIRouter,Depends
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role,require_interactive_session
from modules.orders.routes.show_orders import PrivateRoute
from modules.orders import material_purchases as service
from modules.orders.material_schemas import MaterialInput,AdoptInput,MappingInput,PurchaseInput,CSVInput,ReceiptInput
class MaterialRoute(PrivateRoute):invalid_detail='Invalid material details. Check required fields, whole-piece quantities, ISO dates and USD cents.'
reader=require_role('operator',scope='read');writer=require_role('operator',scope='write');admin=require_role('admin',scope='write')
router=APIRouter(prefix='/consumables/purchases',tags=['Material purchases and receiving'],route_class=MaterialRoute,dependencies=[Depends(reader),Depends(require_interactive_session())])
@router.get('')
def read(user=Depends(reader),db:Session=Depends(get_db)):return service.read(db,user)
@router.post('/materials')
def material(data:MaterialInput,user=Depends(admin),db:Session=Depends(get_db)):return service.register(db,user,data)
@router.post('/adopt')
def adopt(data:AdoptInput,user=Depends(admin),db:Session=Depends(get_db)):return service.register(db,user,data,adopt=True)
@router.post('/mappings')
def mapping(data:MappingInput,user=Depends(admin),db:Session=Depends(get_db)):return service.add_mapping(db,user,data)
@router.post('/preview')
def preview(data:CSVInput,user=Depends(reader),db:Session=Depends(get_db)):return service.preview_csv(db,user,data)
@router.post('/lines')
def purchases(data:PurchaseInput,user=Depends(writer),db:Session=Depends(get_db)):return service.purchases(db,user,data)
@router.post('/receipts')
def receipt(data:ReceiptInput,user=Depends(writer),db:Session=Depends(get_db)):return service.receipt(db,user,data)
