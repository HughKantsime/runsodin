"""O.D.I.N. — Orders CRUD, Line Items, Schedule, Invoice, and Ship."""

from fastapi import APIRouter, Depends, HTTPException, status, Response
from fastapi.responses import Response as FastAPIResponse
from sqlalchemy.orm import Session, joinedload
from typing import List, Optional
from datetime import datetime, timezone
import logging

from core.db import get_db
from core.rbac import require_role
from core.base import OrderStatus, JobStatus
from modules.orders.models import Product, Order, OrderItem
from modules.inventory.models import Consumable, ConsumableUsage
from modules.jobs.models import Job
from modules.orders.schemas import (
    OrderResponse, OrderCreate, OrderUpdate, OrderSummary,
    OrderItemResponse, OrderItemCreate, OrderItemUpdate, OrderShipRequest,
)

log = logging.getLogger("odin.api")

router = APIRouter(prefix="/orders", tags=["Orders"])


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


# -------------- Orders CRUD --------------

@router.get("", response_model=List[OrderSummary])
def list_orders(
    status_filter: Optional[str] = None,
    platform: Optional[str] = None,
    current_user: dict = Depends(require_role("viewer")),
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


@router.post("", response_model=OrderResponse)
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


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(order_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get an order with items and P&L calculation."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    return _enrich_order_response(order, db)


@router.patch("/{order_id}", response_model=OrderResponse)
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


@router.delete("/{order_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order(order_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete an order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    db.delete(order)
    db.commit()


# -------------- Order Items --------------

@router.post("/{order_id}/items", response_model=OrderItemResponse)
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
        unit_price=data.unit_price if data.unit_price is not None else product.price
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


@router.patch("/{order_id}/items/{item_id}", response_model=OrderItemResponse)
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

    resp = OrderItemResponse.model_validate(item)
    product = db.query(Product).filter(Product.id == item.product_id).first()
    if product:
        resp.product_name = product.name
        resp.product_sku = product.sku
    resp.subtotal = (item.unit_price or 0) * item.quantity
    resp.is_fulfilled = item.fulfilled_quantity >= item.quantity
    return resp


@router.delete("/{order_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order_item(order_id: int, item_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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

@router.post("/{order_id}/schedule")
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
            consumable.updated_at = datetime.now(timezone.utc)
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


@router.get("/{order_id}/invoice.pdf")
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
        from modules.organizations.branding import get_or_create_branding, branding_to_dict
        enriched = _enrich_order_response(order, db)
        branding = branding_to_dict(get_or_create_branding(db))

        from modules.orders.invoice_generator import InvoiceGenerator
        gen = InvoiceGenerator(branding, enriched.model_dump())
        pdf_bytes = bytes(gen.generate())
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log.error(f"PDF generation failed for order {order_id}: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")

    return FastAPIResponse(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=invoice_{order.order_number or order.id}.pdf",
            "Content-Length": str(len(pdf_bytes)),
        }
    )


@router.patch("/{order_id}/ship", response_model=OrderResponse)
def ship_order(order_id: int, data: OrderShipRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Mark an order as shipped."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.status = OrderStatus.SHIPPED
    order.tracking_number = data.tracking_number
    order.shipped_date = data.shipped_date or datetime.now(timezone.utc)

    db.commit()
    db.refresh(order)
    return _enrich_order_response(order, db)
