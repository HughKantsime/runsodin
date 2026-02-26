"""
modules/orders/schemas.py — Pydantic schemas for the orders domain.
"""

from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel, Field, ConfigDict

from core.base import OrderStatus


# ============== Product Schemas ==============

class ProductComponentBase(BaseModel):
    model_id: int
    quantity_needed: int = 1
    notes: Optional[str] = None


class ProductComponentCreate(ProductComponentBase):
    pass


class ProductComponentResponse(ProductComponentBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    # Include model name for display
    model_name: Optional[str] = None


class ProductBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    sku: Optional[str] = None
    price: Optional[float] = None
    description: Optional[str] = None


class ProductCreate(ProductBase):
    components: Optional[List[ProductComponentCreate]] = None


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    sku: Optional[str] = None
    price: Optional[float] = None
    description: Optional[str] = None


class ProductResponse(ProductBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime
    components: List[ProductComponentResponse] = []
    consumables: List[Any] = []  # List[ProductConsumableResponse] — populated by API layer

    # Calculated fields (populated by API)
    estimated_cogs: Optional[float] = None
    component_count: Optional[int] = None


class ProductSummary(BaseModel):
    """Lighter product response for dropdowns."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    sku: Optional[str] = None
    price: Optional[float] = None


# ============== Order Schemas ==============

class OrderItemBase(BaseModel):
    product_id: int
    quantity: int = 1
    unit_price: Optional[float] = None


class OrderItemCreate(OrderItemBase):
    pass


class OrderItemUpdate(BaseModel):
    quantity: Optional[int] = None
    unit_price: Optional[float] = None


class OrderItemResponse(OrderItemBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    fulfilled_quantity: int = 0
    created_at: datetime

    # Include product name for display
    product_name: Optional[str] = None
    product_sku: Optional[str] = None

    # Calculated
    subtotal: Optional[float] = None
    is_fulfilled: Optional[bool] = None


class OrderBase(BaseModel):
    order_number: Optional[str] = None
    platform: Optional[str] = None
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    order_date: Optional[datetime] = None
    notes: Optional[str] = None

    # Financials
    revenue: Optional[float] = None
    platform_fees: Optional[float] = None
    payment_fees: Optional[float] = None
    shipping_charged: Optional[float] = None
    shipping_cost: Optional[float] = None
    labor_minutes: Optional[int] = 0


class OrderCreate(OrderBase):
    items: Optional[List[OrderItemCreate]] = None


class OrderUpdate(BaseModel):
    order_number: Optional[str] = None
    platform: Optional[str] = None
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    status: Optional[OrderStatus] = None
    order_date: Optional[datetime] = None
    shipped_date: Optional[datetime] = None
    tracking_number: Optional[str] = None
    notes: Optional[str] = None

    # Financials
    revenue: Optional[float] = None
    platform_fees: Optional[float] = None
    payment_fees: Optional[float] = None
    shipping_charged: Optional[float] = None
    shipping_cost: Optional[float] = None
    labor_minutes: Optional[int] = None


class OrderResponse(OrderBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: OrderStatus
    shipped_date: Optional[datetime] = None
    tracking_number: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    items: List[OrderItemResponse] = []

    # Calculated P&L fields (populated by API)
    total_items: Optional[int] = None
    fulfilled_items: Optional[int] = None
    estimated_cost: Optional[float] = None
    actual_cost: Optional[float] = None
    profit: Optional[float] = None
    margin_percent: Optional[float] = None
    jobs_total: Optional[int] = None
    jobs_complete: Optional[int] = None


class OrderSummary(BaseModel):
    """Lighter order response for lists."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_number: Optional[str] = None
    platform: Optional[str] = None
    customer_name: Optional[str] = None
    status: OrderStatus
    revenue: Optional[float] = None
    order_date: Optional[datetime] = None
    item_count: int = 0
    fulfilled: bool = False


class OrderShipRequest(BaseModel):
    """Request to mark order as shipped."""
    tracking_number: Optional[str] = None
    shipped_date: Optional[datetime] = None  # Defaults to now
