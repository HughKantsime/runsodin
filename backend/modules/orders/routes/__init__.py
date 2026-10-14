"""Orders routes package — assembles all sub-routers."""

from fastapi import APIRouter
from .attempt_valuation import router as attempt_valuation_router
from .finance_settings import router as finance_settings_router
from .products import router as products_router
from .orders_crud import router as orders_crud_router
from .consumables import router as consumables_router

router = APIRouter()
router.include_router(attempt_valuation_router)
router.include_router(products_router)
router.include_router(finance_settings_router)
router.include_router(orders_crud_router)
router.include_router(consumables_router)
