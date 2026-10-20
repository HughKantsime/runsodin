"""Orders routes package — assembles all sub-routers."""

from fastapi import APIRouter, Depends

from license_manager import feature_gate
from .attempt_valuation import router as attempt_valuation_router
from .finance_settings import router as finance_settings_router
from .products import router as products_router
from .square import router as square_router
from .batches import router as batches_router
from .orders_crud import router as orders_crud_router
from .show_orders import router as show_orders_router
from .consumables import router as consumables_router

router = APIRouter()
router.include_router(attempt_valuation_router)
# Pro production features. Existing records stay readable/deletable on every
# tier (downgrade policy, see license_manager.feature_gate).
router.include_router(batches_router, dependencies=[Depends(feature_gate("products", keep_existing_records=True))])
router.include_router(square_router, dependencies=[Depends(feature_gate("products", keep_existing_records=True))])
router.include_router(products_router, dependencies=[Depends(feature_gate("products", keep_existing_records=True))])
router.include_router(finance_settings_router)
router.include_router(show_orders_router, dependencies=[Depends(feature_gate("orders", keep_existing_records=True))])
router.include_router(orders_crud_router, dependencies=[Depends(feature_gate("orders", keep_existing_records=True))])
router.include_router(consumables_router, dependencies=[Depends(feature_gate("products", keep_existing_records=True))])
