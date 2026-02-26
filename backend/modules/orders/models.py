"""
modules/orders/models.py — ORM models for the orders domain.

Owns tables: orders, order_items, products, product_components
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, Enum as SQLEnum, Text
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from core.base import Base, OrderStatus, _ENUM_VALUES


class Product(Base):
    """
    A sellable product in your catalog.

    Can be simple (single print) or complex (multiple prints/assembly).
    The Bill of Materials (BOM) is stored in ProductComponent.
    """
    __tablename__ = "products"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    sku = Column(String(50), nullable=True)  # Optional SKU/part number
    price = Column(Float, nullable=True)      # Default selling price
    description = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    components = relationship("ProductComponent", back_populates="product", cascade="all, delete-orphan")
    order_items = relationship("OrderItem", back_populates="product")

    def __repr__(self):
        return f"<Product {self.name}>"


class ProductComponent(Base):
    """
    Bill of Materials entry - links a Model to a Product.

    Example: Golf Ball Dispenser needs 1x base, 1x tube, 4x brackets, 4x feet.
    Each of those is a ProductComponent with quantity_needed set.
    """
    __tablename__ = "product_components"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    model_id = Column(Integer, ForeignKey("models.id"), nullable=False)
    quantity_needed = Column(Integer, default=1)  # How many of this part per product
    notes = Column(Text, nullable=True)

    # Relationships
    product = relationship("Product", back_populates="components")
    model = relationship("Model")

    def __repr__(self):
        return f"<ProductComponent {self.product_id}:{self.model_id} x{self.quantity_needed}>"


class Order(Base):
    """
    A customer purchase/order.

    Can come from Etsy, Amazon, wholesale, direct sales, etc.
    Contains line items (OrderItem) and tracks fulfillment status.
    """
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    order_number = Column(String(100), nullable=True)  # External ref (Etsy order #)
    platform = Column(String(50), nullable=True)       # 'etsy', 'amazon', 'direct', 'wholesale'

    # Customer info
    customer_name = Column(String(200), nullable=True)
    customer_email = Column(String(200), nullable=True)

    # Status tracking
    status = Column(SQLEnum(OrderStatus, values_callable=_ENUM_VALUES), default=OrderStatus.PENDING)

    # Financials - what you charged
    revenue = Column(Float, nullable=True)             # Total charged to customer
    platform_fees = Column(Float, nullable=True)       # Etsy/Amazon cut
    payment_fees = Column(Float, nullable=True)        # Stripe/PayPal cut
    shipping_charged = Column(Float, nullable=True)    # What customer paid for shipping
    shipping_cost = Column(Float, nullable=True)       # What you paid for label

    # Labor tracking (order-level for MVP)
    labor_minutes = Column(Integer, default=0)         # Packing/shipping labor

    # Notes
    notes = Column(Text, nullable=True)

    # Dates
    order_date = Column(DateTime, nullable=True)
    shipped_date = Column(DateTime, nullable=True)
    tracking_number = Column(String(100), nullable=True)

    # Timestamps
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Order {self.order_number or self.id} ({self.status.value})>"


class OrderItem(Base):
    """
    A line item on an order.

    Example: "2x Baby Yoda @ $15 each"
    Tracks fulfillment progress as jobs complete.
    """
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)

    quantity = Column(Integer, default=1)              # How many ordered
    unit_price = Column(Float, nullable=True)          # Price per unit on THIS order
    fulfilled_quantity = Column(Integer, default=0)    # How many completed so far

    # Timestamps
    created_at = Column(DateTime, server_default=func.now())

    # Relationships
    order = relationship("Order", back_populates="items")
    product = relationship("Product", back_populates="order_items")
    jobs = relationship("Job", backref="order_item")

    def __repr__(self):
        return f"<OrderItem {self.order_id}:{self.product_id} x{self.quantity}>"
