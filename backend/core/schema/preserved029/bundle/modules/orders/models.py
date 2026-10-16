"""
modules/orders/models.py — ORM models for the orders domain.

Owns tables: orders, order_items, products, product_components
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, Boolean,
    ForeignKey, ForeignKeyConstraint, CheckConstraint, Enum as SQLEnum, Text
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

    # Multi-tenant scoping
    org_id = Column(Integer, nullable=True)

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
    model_id = Column(Integer, ForeignKey("models.id", ondelete="CASCADE"), nullable=False)
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

    # Multi-tenant scoping
    org_id = Column(Integer, nullable=True)

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


class OrderProductionItem(Base):
    """Immutable scheduled requirements; explicit confirmation is separate."""
    __tablename__ = "order_production_items"
    order_item_id = Column(Integer, ForeignKey("order_items.id", ondelete="RESTRICT"), primary_key=True, autoincrement=False)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False)
    product_id = Column(Integer, nullable=False)
    ordered_quantity = Column(Integer, nullable=False)
    mode = Column(Text, nullable=False)
    confirmed_quantity = Column(Integer, nullable=False, server_default="0")
    __table_args__ = (
        CheckConstraint("ordered_quantity > 0", name="opi_positive_quantity"),
        CheckConstraint("confirmed_quantity >= 0 AND confirmed_quantity <= ordered_quantity", name="opi_confirmation_bounds"),
        CheckConstraint("mode IN ('printed','mixed','consumable_only','historical')", name="opi_mode"),
    )


class OrderProductionComponent(Base):
    __tablename__ = "order_production_components"
    order_item_id = Column(Integer, ForeignKey("order_production_items.order_item_id", ondelete="RESTRICT"), primary_key=True, autoincrement=False)
    model_id = Column(Integer, primary_key=True, autoincrement=False)
    pieces_per_unit = Column(Integer, nullable=False)
    __table_args__ = (CheckConstraint("pieces_per_unit > 0", name="opc_positive_pieces"),)


class OrderProductionJob(Base):
    """Original job identity survives deletion; replacements are not inferred."""
    __tablename__ = "order_production_jobs"
    job_id = Column(Integer, primary_key=True, autoincrement=False)
    order_item_id = Column(Integer, nullable=False)
    model_id = Column(Integer, nullable=False)
    piece_count = Column(Integer, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["order_item_id", "model_id"], ["order_production_components.order_item_id", "order_production_components.model_id"], ondelete="RESTRICT"),
        CheckConstraint("piece_count > 0", name="opj_positive_pieces"),
    )
