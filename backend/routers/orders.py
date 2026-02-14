"""O.D.I.N. — Orders, Products & Consumables Routes"""
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request, Response
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text
from typing import List, Optional
from datetime import datetime
import json
import logging

from deps import get_db, get_current_user, require_role, log_audit, _get_org_filter
from models import (
    Product, ProductComponent, Order, OrderItem, OrderStatus,
    Consumable, ProductConsumable, ConsumableUsage,
    Model, Job, JobStatus, SystemConfig, FilamentLibrary,
)
from schemas import (
    ProductResponse, ProductCreate, ProductUpdate,
    ProductComponentResponse, ProductComponentCreate,
    ProductConsumableResponse, ProductConsumableCreate,
    OrderResponse, OrderCreate, OrderUpdate, OrderSummary,
    OrderItemResponse, OrderItemCreate, OrderItemUpdate, OrderShipRequest,
    ConsumableCreate, ConsumableUpdate, ConsumableResponse, ConsumableAdjust,
)
from config import settings

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== Pricing Config ==============

DEFAULT_PRICING_CONFIG = {
    "spool_cost": 25.0,
    "spool_weight": 1000.0,
    "hourly_rate": 15.0,
    "electricity_rate": 0.12,
    "printer_wattage": 100,
    "printer_cost": 300.0,
    "printer_lifespan": 5000,
    "packaging_cost": 0.45,
    "failure_rate": 7.0,
    "monthly_rent": 0.0,
    "parts_per_month": 100,
    "post_processing_min": 5,
    "packing_min": 5,
    "support_min": 5,
    "default_margin": 50.0,
    "other_costs": 0.0,
    "ui_mode": "advanced"
}


def calculate_job_cost(db: Session, model_id: int = None, filament_grams: float = 0, print_hours: float = 1.0, material_type: str = "PLA"):
    """Calculate estimated cost and suggested price for a job.

    Returns tuple: (estimated_cost, suggested_price, margin_percent)
    """
    # Get pricing config
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG

    # Get model for defaults if provided
    model = None
    if model_id:
        model = db.query(Model).filter(Model.id == model_id).first()
        if model:
            filament_grams = filament_grams or model.total_filament_grams or 0
            print_hours = print_hours or model.build_time_hours or 1.0
            material_type = model.default_filament_type.value if model.default_filament_type else "PLA"

    # Try to get per-material cost
    filament_entry = db.query(FilamentLibrary).filter(
        FilamentLibrary.material == material_type,
        FilamentLibrary.cost_per_gram.isnot(None)
    ).first()

    if filament_entry and filament_entry.cost_per_gram:
        cost_per_gram = filament_entry.cost_per_gram
    else:
        cost_per_gram = config["spool_cost"] / config["spool_weight"]

    # Calculate costs
    material_cost = filament_grams * cost_per_gram
    labor_hours = (config["post_processing_min"] + config["packing_min"] + config["support_min"]) / 60
    labor_cost = labor_hours * config["hourly_rate"]
    electricity_cost = (config["printer_wattage"] / 1000) * print_hours * config["electricity_rate"]
    depreciation_cost = (config["printer_cost"] / config["printer_lifespan"]) * print_hours
    packaging_cost = config["packaging_cost"]
    base_cost = material_cost + labor_cost + electricity_cost + depreciation_cost + packaging_cost + config["other_costs"]
    failure_cost = base_cost * (config["failure_rate"] / 100)
    overhead_cost = config["monthly_rent"] / config["parts_per_month"] if config["parts_per_month"] > 0 else 0

    subtotal = base_cost + failure_cost + overhead_cost

    margin = model.markup_percent if model and model.markup_percent else config["default_margin"]
    suggested_price = subtotal * (1 + margin / 100)

    return (round(subtotal, 2), round(suggested_price, 2), margin)


# -------------- Products --------------

@router.get("/api/products", response_model=List[ProductResponse], tags=["Products"])
def list_products(db: Session = Depends(get_db)):
    """List all products."""
    products = db.query(Product).all()
    result = []
    for p in products:
        resp = ProductResponse.model_validate(p)
        resp.component_count = len(p.components)
        # Calculate estimated COGS from printed components + consumables
        cogs = 0
        for comp in p.components:
            if comp.model and comp.model.cost_per_item:
                cogs += comp.model.cost_per_item * comp.quantity_needed
        for pc in getattr(p, 'consumable_links', []):
            if pc.consumable and pc.consumable.cost_per_unit:
                cogs += pc.consumable.cost_per_unit * pc.quantity_per_product
        resp.estimated_cogs = round(cogs, 2) if cogs > 0 else None
        # Enrich consumables
        resp.consumables = [
            ProductConsumableResponse(id=pc.id, product_id=pc.product_id, consumable_id=pc.consumable_id,
                                      quantity_per_product=pc.quantity_per_product, notes=pc.notes,
                                      consumable_name=pc.consumable.name if pc.consumable else None)
            for pc in getattr(p, 'consumable_links', [])
        ]
        result.append(resp)
    return result


@router.post("/api/products", response_model=ProductResponse, tags=["Products"])
def create_product(data: ProductCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Create a new product with optional BOM components."""
    product = Product(
        name=data.name,
        sku=data.sku,
        price=data.price,
        description=data.description
    )
    db.add(product)
    db.flush()  # Get the ID before adding components

    # Add components if provided
    if data.components:
        for comp_data in data.components:
            comp = ProductComponent(
                product_id=product.id,
                model_id=comp_data.model_id,
                quantity_needed=comp_data.quantity_needed,
                notes=comp_data.notes
            )
            db.add(comp)

    db.commit()
    db.refresh(product)
    return product


@router.get("/api/products/{product_id}", response_model=ProductResponse, tags=["Products"])
def get_product(product_id: int, db: Session = Depends(get_db)):
    """Get a product with its BOM components."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    resp = ProductResponse.model_validate(product)
    resp.component_count = len(product.components)

    # Enrich components with model names
    enriched_components = []
    cogs = 0
    for comp in product.components:
        comp_resp = ProductComponentResponse.model_validate(comp)
        if comp.model:
            comp_resp.model_name = comp.model.name
            if comp.model.cost_per_item:
                cogs += comp.model.cost_per_item * comp.quantity_needed
        enriched_components.append(comp_resp)
    resp.components = enriched_components

    # Enrich consumables and add to COGS
    resp.consumables = []
    for pc in getattr(product, 'consumable_links', []):
        pc_resp = ProductConsumableResponse(
            id=pc.id, product_id=pc.product_id, consumable_id=pc.consumable_id,
            quantity_per_product=pc.quantity_per_product, notes=pc.notes,
            consumable_name=pc.consumable.name if pc.consumable else None
        )
        resp.consumables.append(pc_resp)
        if pc.consumable and pc.consumable.cost_per_unit:
            cogs += pc.consumable.cost_per_unit * pc.quantity_per_product

    resp.estimated_cogs = round(cogs, 2) if cogs > 0 else None
    return resp


@router.patch("/api/products/{product_id}", response_model=ProductResponse, tags=["Products"])
def update_product(product_id: int, data: ProductUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update a product."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(product, key, value)

    db.commit()
    db.refresh(product)
    return product


@router.delete("/api/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Products"])
def delete_product(product_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete a product."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    db.delete(product)
    db.commit()


# -------------- Product Components (BOM) --------------

@router.post("/api/products/{product_id}/components", response_model=ProductComponentResponse, tags=["Products"])
def add_product_component(product_id: int, data: ProductComponentCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Add a component to a product's BOM."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    model = db.query(Model).filter(Model.id == data.model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")

    comp = ProductComponent(
        product_id=product_id,
        model_id=data.model_id,
        quantity_needed=data.quantity_needed,
        notes=data.notes
    )
    db.add(comp)
    db.commit()
    db.refresh(comp)

    resp = ProductComponentResponse.model_validate(comp)
    resp.model_name = model.name
    return resp


@router.delete("/api/products/{product_id}/components/{component_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Products"])
def remove_product_component(product_id: int, component_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Remove a component from a product's BOM."""
    comp = db.query(ProductComponent).filter(
        ProductComponent.id == component_id,
        ProductComponent.product_id == product_id
    ).first()
    if not comp:
        raise HTTPException(status_code=404, detail="Component not found")

    db.delete(comp)
    db.commit()


# -------------- Orders --------------

@router.get("/api/orders", response_model=List[OrderSummary], tags=["Orders"])
def list_orders(
    status_filter: Optional[str] = None,
    platform: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List all orders with optional filters."""
    query = db.query(Order)

    if status_filter:
        query = query.filter(Order.status == status_filter)
    if platform:
        query = query.filter(Order.platform == platform)

    orders = query.order_by(Order.created_at.desc()).all()

    result = []
    for o in orders:
        summary = OrderSummary(
            id=o.id,
            order_number=o.order_number,
            platform=o.platform,
            customer_name=o.customer_name,
            status=o.status,
            revenue=o.revenue,
            order_date=o.order_date,
            item_count=len(o.items),
            fulfilled=all(item.fulfilled_quantity >= item.quantity for item in o.items) if o.items else False
        )
        result.append(summary)
    return result


@router.post("/api/orders", response_model=OrderResponse, tags=["Orders"])
def create_order(data: OrderCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Create a new order with optional line items."""
    order = Order(
        order_number=data.order_number,
        platform=data.platform,
        customer_name=data.customer_name,
        customer_email=data.customer_email,
        order_date=data.order_date,
        notes=data.notes,
        revenue=data.revenue,
        platform_fees=data.platform_fees,
        payment_fees=data.payment_fees,
        shipping_charged=data.shipping_charged,
        shipping_cost=data.shipping_cost,
        labor_minutes=data.labor_minutes or 0
    )
    db.add(order)
    db.flush()

    # Add line items if provided
    if data.items:
        for item_data in data.items:
            # Verify product exists
            product = db.query(Product).filter(Product.id == item_data.product_id).first()
            if not product:
                raise HTTPException(status_code=404, detail=f"Product {item_data.product_id} not found")

            item = OrderItem(
                order_id=order.id,
                product_id=item_data.product_id,
                quantity=item_data.quantity,
                unit_price=item_data.unit_price if item_data.unit_price else product.price
            )
            db.add(item)

    db.commit()
    db.refresh(order)
    return _enrich_order_response(order, db)


@router.get("/api/orders/{order_id}", response_model=OrderResponse, tags=["Orders"])
def get_order(order_id: int, db: Session = Depends(get_db)):
    """Get an order with items and P&L calculation."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return _enrich_order_response(order, db)


@router.patch("/api/orders/{order_id}", response_model=OrderResponse, tags=["Orders"])
def update_order(order_id: int, data: OrderUpdate, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update an order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(order, key, value)

    db.commit()
    db.refresh(order)
    return _enrich_order_response(order, db)


@router.delete("/api/orders/{order_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Orders"])
def delete_order(order_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete an order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    db.delete(order)
    db.commit()


# -------------- Order Items --------------

@router.post("/api/orders/{order_id}/items", response_model=OrderItemResponse, tags=["Orders"])
def add_order_item(order_id: int, data: OrderItemCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Add a line item to an order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    product = db.query(Product).filter(Product.id == data.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    item = OrderItem(
        order_id=order_id,
        product_id=data.product_id,
        quantity=data.quantity,
        unit_price=data.unit_price if data.unit_price else product.price
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    resp = OrderItemResponse.model_validate(item)
    resp.product_name = product.name
    resp.product_sku = product.sku
    resp.subtotal = (item.unit_price or 0) * item.quantity
    resp.is_fulfilled = item.fulfilled_quantity >= item.quantity
    return resp


@router.patch("/api/orders/{order_id}/items/{item_id}", response_model=OrderItemResponse, tags=["Orders"])
def update_order_item(order_id: int, item_id: int, data: OrderItemUpdate, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update an order line item."""
    item = db.query(OrderItem).filter(
        OrderItem.id == item_id,
        OrderItem.order_id == order_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Order item not found")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(item, key, value)

    db.commit()
    db.refresh(item)

    product = db.query(Product).filter(Product.id == item.product_id).first()
    resp = OrderItemResponse.model_validate(item)
    if product:
        resp.product_name = product.name
        resp.product_sku = product.sku
    resp.subtotal = (item.unit_price or 0) * item.quantity
    resp.is_fulfilled = item.fulfilled_quantity >= item.quantity
    return resp


@router.delete("/api/orders/{order_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Orders"])
def remove_order_item(order_id: int, item_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Remove a line item from an order."""
    item = db.query(OrderItem).filter(
        OrderItem.id == item_id,
        OrderItem.order_id == order_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Order item not found")

    db.delete(item)
    db.commit()


# -------------- Order Actions --------------

@router.post("/api/orders/{order_id}/schedule", tags=["Orders"])
def schedule_order(order_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Generate jobs for an order based on BOM."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    jobs_created = []

    for item in order.items:
        product = item.product
        if not product or not product.components:
            continue

        # For each component in the BOM
        for comp in product.components:
            model = comp.model
            if not model:
                continue

            # Calculate how many jobs needed
            pieces_needed = item.quantity * comp.quantity_needed
            pieces_per_job = model.quantity_per_bed or 1
            jobs_needed = -(-pieces_needed // pieces_per_job)  # Ceiling division

            # Create jobs
            for i in range(jobs_needed):
                qty_this_job = min(pieces_per_job, pieces_needed - (i * pieces_per_job))

                job = Job(
                    model_id=model.id,
                    item_name=f"{model.name} (Order #{order.order_number or order.id})",
                    quantity=1,
                    order_item_id=item.id,
                    quantity_on_bed=qty_this_job,
                    status=JobStatus.PENDING,
                    duration_hours=model.build_time_hours,
                    filament_type=model.default_filament_type
                )
                db.add(job)
                jobs_created.append({
                    "model": model.name,
                    "quantity_on_bed": qty_this_job
                })

    # Deduct consumables from inventory
    consumable_deductions = []
    for item in order.items:
        product = item.product
        if not product:
            continue
        for pc in getattr(product, 'consumable_links', []):
            consumable = pc.consumable
            if not consumable or consumable.status != 'active':
                continue
            total_needed = pc.quantity_per_product * item.quantity
            consumable.current_stock = max(0, consumable.current_stock - total_needed)
            consumable.updated_at = datetime.utcnow()
            usage = ConsumableUsage(
                consumable_id=consumable.id,
                order_id=order.id,
                quantity_used=total_needed,
                notes=f"Auto-deducted for Order #{order.order_number or order.id}"
            )
            db.add(usage)
            consumable_deductions.append({
                "consumable": consumable.name,
                "quantity_deducted": total_needed,
                "remaining_stock": consumable.current_stock
            })

    # Update order status
    if jobs_created or consumable_deductions:
        order.status = OrderStatus.IN_PROGRESS

    db.commit()

    return {
        "success": True,
        "order_id": order_id,
        "jobs_created": len(jobs_created),
        "details": jobs_created,
        "consumables_deducted": consumable_deductions
    }


@router.get("/api/orders/{order_id}/invoice.pdf", tags=["Orders"])
def get_order_invoice(
    order_id: int,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db)
):
    """Generate a branded PDF invoice for an order."""
    order = db.query(Order).options(joinedload(Order.items)).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        from branding import get_or_create_branding, branding_to_dict
        enriched = _enrich_order_response(order, db)
        branding = branding_to_dict(get_or_create_branding(db))

        from invoice_generator import InvoiceGenerator
        gen = InvoiceGenerator(branding, enriched.model_dump())
        pdf_bytes = bytes(gen.generate())
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[invoice] PDF generation failed for order {order_id}: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")

    from fastapi.responses import Response as FastAPIResponse
    return FastAPIResponse(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=invoice_{order.order_number or order.id}.pdf",
            "Content-Length": str(len(pdf_bytes)),
        }
    )


@router.patch("/api/orders/{order_id}/ship", response_model=OrderResponse, tags=["Orders"])
def ship_order(order_id: int, data: OrderShipRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Mark an order as shipped."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.status = OrderStatus.SHIPPED
    order.tracking_number = data.tracking_number
    order.shipped_date = data.shipped_date or datetime.utcnow()

    db.commit()
    db.refresh(order)
    return _enrich_order_response(order, db)


# -------------- Helper Functions --------------

def _enrich_order_response(order: Order, db: Session) -> OrderResponse:
    """Build a full OrderResponse with calculated fields."""
    resp = OrderResponse.model_validate(order)

    # Enrich items
    enriched_items = []
    total_items = 0
    fulfilled_items = 0

    for item in order.items:
        item_resp = OrderItemResponse.model_validate(item)
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if product:
            item_resp.product_name = product.name
            item_resp.product_sku = product.sku
        item_resp.subtotal = (item.unit_price or 0) * item.quantity
        item_resp.is_fulfilled = item.fulfilled_quantity >= item.quantity
        enriched_items.append(item_resp)

        total_items += item.quantity
        fulfilled_items += min(item.fulfilled_quantity, item.quantity)

    resp.items = enriched_items
    resp.total_items = total_items
    resp.fulfilled_items = fulfilled_items

    # Count jobs
    jobs = db.query(Job).join(OrderItem).filter(OrderItem.order_id == order.id).all()
    resp.jobs_total = len(jobs)
    resp.jobs_complete = len([j for j in jobs if j.status == JobStatus.COMPLETED])

    # Calculate costs from jobs
    estimated_cost = sum(j.estimated_cost or 0 for j in jobs)
    actual_cost = sum(j.estimated_cost or 0 for j in jobs if j.status == JobStatus.COMPLETED)

    # Add fees and shipping
    total_fees = (order.platform_fees or 0) + (order.payment_fees or 0) + (order.shipping_cost or 0)

    resp.estimated_cost = round(estimated_cost + total_fees, 2) if estimated_cost else None
    resp.actual_cost = round(actual_cost + total_fees, 2) if actual_cost else None

    # Calculate profit
    if order.revenue and resp.actual_cost:
        resp.profit = round(order.revenue - resp.actual_cost, 2)
        resp.margin_percent = round((resp.profit / order.revenue) * 100, 1) if order.revenue > 0 else None

    return resp


# ============== Consumables ==============

@router.get("/api/consumables", tags=["Consumables"])
def list_consumables(status: str = None, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """List all consumables with low-stock flag."""
    query = db.query(Consumable)
    if status:
        query = query.filter(Consumable.status == status)
    items = query.order_by(Consumable.name).all()
    result = []
    for c in items:
        resp = ConsumableResponse.model_validate(c)
        resp.is_low_stock = c.current_stock < c.min_stock if c.min_stock and c.min_stock > 0 else False
        result.append(resp)
    return result


@router.post("/api/consumables", tags=["Consumables"])
def create_consumable(data: ConsumableCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Create a new consumable item."""
    item = Consumable(**data.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    resp = ConsumableResponse.model_validate(item)
    resp.is_low_stock = item.current_stock < item.min_stock if item.min_stock and item.min_stock > 0 else False
    return resp


@router.get("/api/consumables/low-stock", tags=["Consumables"])
def low_stock_consumables(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get consumables below their minimum stock threshold."""
    items = db.query(Consumable).filter(
        Consumable.status == "active",
        Consumable.min_stock > 0,
        Consumable.current_stock < Consumable.min_stock
    ).all()
    result = []
    for c in items:
        resp = ConsumableResponse.model_validate(c)
        resp.is_low_stock = True
        result.append(resp)
    return result


@router.get("/api/consumables/{consumable_id}", tags=["Consumables"])
def get_consumable(consumable_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get a consumable with recent usage history."""
    item = db.query(Consumable).filter(Consumable.id == consumable_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Consumable not found")
    resp = ConsumableResponse.model_validate(item)
    resp.is_low_stock = item.current_stock < item.min_stock if item.min_stock and item.min_stock > 0 else False
    # Include recent usage
    usage = db.query(ConsumableUsage).filter(
        ConsumableUsage.consumable_id == consumable_id
    ).order_by(ConsumableUsage.used_at.desc()).limit(50).all()
    return {
        **resp.model_dump(),
        "usage_history": [{"id": u.id, "quantity_used": u.quantity_used, "used_at": u.used_at,
                           "order_id": u.order_id, "notes": u.notes} for u in usage]
    }


@router.patch("/api/consumables/{consumable_id}", tags=["Consumables"])
def update_consumable(consumable_id: int, data: ConsumableUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update a consumable."""
    item = db.query(Consumable).filter(Consumable.id == consumable_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Consumable not found")
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(item, key, val)
    item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    resp = ConsumableResponse.model_validate(item)
    resp.is_low_stock = item.current_stock < item.min_stock if item.min_stock and item.min_stock > 0 else False
    return resp


@router.delete("/api/consumables/{consumable_id}", tags=["Consumables"])
def delete_consumable(consumable_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Delete a consumable."""
    item = db.query(Consumable).filter(Consumable.id == consumable_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Consumable not found")
    db.delete(item)
    db.commit()
    return {"success": True}


@router.post("/api/consumables/{consumable_id}/adjust", tags=["Consumables"])
def adjust_consumable_stock(consumable_id: int, data: ConsumableAdjust, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Manual stock adjustment (restock or deduct)."""
    item = db.query(Consumable).filter(Consumable.id == consumable_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Consumable not found")
    if data.type == "restock":
        item.current_stock += data.quantity
    else:
        item.current_stock = max(0, item.current_stock - data.quantity)
    usage = ConsumableUsage(
        consumable_id=consumable_id,
        quantity_used=data.quantity if data.type == "deduct" else -data.quantity,
        notes=data.notes or f"Manual {data.type}"
    )
    db.add(usage)
    item.updated_at = datetime.utcnow()
    db.commit()
    return {"success": True, "new_stock": item.current_stock}


@router.post("/api/products/{product_id}/consumables", tags=["Products"])
def add_product_consumable(product_id: int, data: ProductConsumableCreate,
                           current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Add a consumable to a product's BOM."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    consumable = db.query(Consumable).filter(Consumable.id == data.consumable_id).first()
    if not consumable:
        raise HTTPException(status_code=404, detail="Consumable not found")
    link = ProductConsumable(
        product_id=product_id,
        consumable_id=data.consumable_id,
        quantity_per_product=data.quantity_per_product,
        notes=data.notes
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    resp = ProductConsumableResponse.model_validate(link)
    resp.consumable_name = consumable.name
    return resp


@router.delete("/api/products/{product_id}/consumables/{consumable_link_id}", tags=["Products"])
def remove_product_consumable(product_id: int, consumable_link_id: int,
                              current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Remove a consumable from a product's BOM."""
    link = db.query(ProductConsumable).filter(
        ProductConsumable.id == consumable_link_id,
        ProductConsumable.product_id == product_id
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Product consumable not found")
    db.delete(link)
    db.commit()
    return {"success": True}
