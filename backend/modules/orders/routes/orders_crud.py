"""O.D.I.N. — Orders CRUD, Line Items, Schedule, Invoice, and Ship."""

from core.rbac import should_filter_org

from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from fastapi.responses import Response as FastAPIResponse
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import update, text
import math
from typing import List, Optional
from datetime import datetime, timezone
import logging

from core.economic_identity import _add_created
from core.db import get_db
from core.dependencies import log_audit
from core.rbac import (
    AGENT_READ_SCOPE,
    AGENT_WRITE_SCOPE,
    check_org_access,
    get_org_scope,
    require_any_scope,
    require_role,
)
from core.base import OrderStatus, JobStatus
from modules.orders.models import Product, ProductComponent, Order, OrderItem
from modules.inventory.models import Consumable, ConsumableUsage, ProductConsumable
from modules.jobs.models import Job
from modules.orders.schemas import (
    OrderResponse, OrderCreate, OrderUpdate, OrderSummary,
    OrderItemResponse, OrderItemCreate, OrderItemUpdate, OrderShipRequest, OrderProductionConfirmation,
)

log = logging.getLogger("odin.api")

router = APIRouter(prefix="/orders", tags=["Orders"])


def _fulfillment():
    from core.registry import registry
    provider=registry.get_provider("OrderFulfillmentProvider")
    if provider is None:raise HTTPException(503,"Order production service unavailable")
    return provider


def _locked_order(db, order_id, current_user):
    _fulfillment().lock_orders(db, [order_id])
    order = db.query(Order).options(selectinload(Order.items)).filter(Order.id == order_id).populate_existing().first()
    if not order or not check_org_access(current_user, order.org_id):
        raise HTTPException(404, "Order not found")
    return order


# -------------- Helper Functions --------------

def _enrich_order_response(order: Order, db: Session) -> OrderResponse:
    """Build a full OrderResponse with calculated fields."""
    resp = OrderResponse.model_validate(order)

    # Enrich items
    enriched_items = []
    total_items = 0
    fulfilled_items = 0

    modes = dict(db.execute(text('SELECT order_item_id,mode FROM order_production_items WHERE order_id=:id'), {'id': order.id}).all())
    for item in order.items:
        item_resp = OrderItemResponse.model_validate(item)
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if product:
            item_resp.product_name = product.name
            item_resp.product_sku = product.sku
        item_resp.subtotal = item.unit_price * item.quantity if item.unit_price is not None else None
        item_resp.is_fulfilled = item.fulfilled_quantity >= item.quantity
        item_resp.production_mode = modes.get(item.id)
        item_resp.can_confirm_production = (item.quantity > 0 and modes.get(item.id) not in ('printed', 'mixed')
                                            and order.status not in ('shipped', 'cancelled'))
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

    # Add fees and shipping
    total_fees = (order.platform_fees or 0) + (order.payment_fees or 0) + (order.shipping_cost or 0)

    has_recorded_fees = any(value is not None for value in (order.platform_fees, order.payment_fees, order.shipping_cost))
    resp.estimated_cost = round(estimated_cost + total_fees, 2) if any(j.estimated_cost is not None for j in jobs) or has_recorded_fees else None
    # Completed job estimates and recorded fees do not prove measured production
    # cost. Keep actual financials unknown until complete historical valuation,
    # obligations and matching currency have an authoritative source.
    resp.actual_cost = None
    resp.profit = None
    resp.margin_percent = None

    return resp


# -------------- Orders CRUD --------------

@router.get("", response_model=List[OrderSummary])
def list_orders(
    status_filter: Optional[str] = None,
    platform: Optional[str] = None,
    # Stacked auth (Phase 2 canonical read shape). Note: the v1.8.9 wiki
    # documents `/api/v1/orders` as returning 500 under some conditions;
    # this Phase 2 retrofit does not alter the handler body. If the 500
    # reproduces under agent traffic, file a separate bugfix track per
    # spec R7. Do not mask the error in this retrofit.
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(
        require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE)
    ),
    db: Session = Depends(get_db),
):
    """List all orders with optional filters."""
    org = get_org_scope(current_user)
    query = db.query(Order)

    if should_filter_org(current_user, org):
        query = query.filter((Order.org_id == org) | (Order.org_id == None))

    if status_filter:
        query = query.filter(Order.status == status_filter)
    if platform:
        query = query.filter(Order.platform == platform)

    orders = query.options(selectinload(Order.items)).order_by(Order.created_at.desc()).all()

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


def _stage_prospective_order_resource(db, resource, kind, actor, owner):
    """Fresh birth attestation uses actual caller authority, never NULL alone."""
    platform = actor.get("role") == "admin" and actor.get("group_id") is None
    authorized = (type(owner) is int and owner > 0 and
                  ((type(actor.get("group_id")) is int and actor.get("group_id") == owner) or platform)) or (owner is None and platform)
    if kind == "item":
        parent_uid = db.execute(text("SELECT economic_uid FROM orders WHERE id=:id"), {"id": resource.order_id}).scalar_one_or_none()
        authorized = authorized and bool(parent_uid)
    elif kind == "job":
        parents = db.execute(text("SELECT i.economic_uid,o.economic_uid FROM order_items i JOIN orders o ON o.id=i.order_id WHERE i.id=:id"), {"id": resource.order_item_id}).first()
        authorized = authorized and parents is not None and bool(parents[0]) and bool(parents[1])
    if authorized:
        return _add_created(db, resource, kind, owner_org_id=owner, global_authority=platform and owner is None)
    db.add(resource)
    db.flush()
    return resource


@router.post("", response_model=OrderResponse)
def create_order(data: OrderCreate, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db), request: Request = None):
    """Create a new order with optional line items."""
    for item in data.items or []:
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if not product or not check_org_access(current_user, product.org_id):
            raise HTTPException(status_code=404, detail="Product not found")
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
        labor_minutes=data.labor_minutes or 0,
        org_id=current_user.get("group_id"),
    )
    from core.global_order_creation import has_verified_origin_request, create_attested_global_order
    if (order.org_id is None and current_user.get('role') == 'admin'
            and has_verified_origin_request(db, request, current_user)):
        order = create_attested_global_order(db, order, request, current_user)
    else:
        _stage_prospective_order_resource(db, order, "order", current_user, order.org_id)

    # Add line items if provided
    if data.items:
        for item_data in data.items:
            # Verify product exists
            product = db.query(Product).filter(Product.id == item_data.product_id).first()
            if not product or not check_org_access(current_user, product.org_id):
                raise HTTPException(status_code=404, detail="Product not found")

            item = OrderItem(
                order_id=order.id,
                product_id=item_data.product_id,
                quantity=item_data.quantity,
                unit_price=item_data.unit_price if item_data.unit_price is not None else product.price
            )
            _stage_prospective_order_resource(db, item, "item", current_user, order.org_id)

    db.commit()
    db.refresh(order)
    return _enrich_order_response(order, db)


@router.get("/{order_id}", response_model=OrderResponse)
def get_order(order_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get an order with items and P&L calculation."""
    order = db.query(Order).options(selectinload(Order.items)).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not check_org_access(current_user, order.org_id):
        raise HTTPException(status_code=404, detail="Order not found")

    return _enrich_order_response(order, db)


@router.patch("/{order_id}", response_model=OrderResponse)
def update_order(order_id: int, data: OrderUpdate, current_user: dict = Depends(require_role("admin", scope="write")), db: Session = Depends(get_db)):
    """Update an order."""
    order = db.query(Order).options(selectinload(Order.items)).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not check_org_access(current_user, order.org_id):
        raise HTTPException(status_code=404, detail="Order not found")

    order = _locked_order(db, order_id, current_user)
    if data.status == OrderStatus.FULFILLED:
        _fulfillment().reconcile_orders(db, [order_id])
        db.expire_all()
        order = _locked_order(db, order_id, current_user)
        if not order.items or not all(item.quantity > 0 and item.fulfilled_quantity >= item.quantity for item in order.items):
            raise HTTPException(409, "Complete scheduled jobs or explicitly confirm production before marking this order fulfilled")

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(order, key, value)

    db.flush()
    _fulfillment().reconcile_orders(db, [order_id])
    db.commit()
    db.refresh(order)
    return _enrich_order_response(order, db)


@router.delete("/{order_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order(order_id: int, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Delete an order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not check_org_access(current_user, order.org_id):
        raise HTTPException(status_code=404, detail="Order not found")
    _fulfillment().assert_order_structure_edit_allowed(db,order_id)

    db.delete(order)
    db.commit()


# -------------- Order Items --------------

@router.post("/{order_id}/items", response_model=OrderItemResponse)
def add_order_item(order_id: int, data: OrderItemCreate, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Add a line item to an order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not check_org_access(current_user, order.org_id):
        raise HTTPException(status_code=404, detail="Order not found")
    _fulfillment().assert_order_structure_edit_allowed(db,order_id)

    product = db.query(Product).filter(Product.id == data.product_id).first()
    if not product or not check_org_access(current_user, product.org_id):
        raise HTTPException(status_code=404, detail="Product not found")

    item = OrderItem(
        order_id=order_id,
        product_id=data.product_id,
        quantity=data.quantity,
        unit_price=data.unit_price if data.unit_price is not None else product.price
    )
    _stage_prospective_order_resource(db, item, "item", current_user, order.org_id)
    db.commit()
    db.refresh(item)

    resp = OrderItemResponse.model_validate(item)
    resp.product_name = product.name
    resp.product_sku = product.sku
    resp.subtotal = item.unit_price * item.quantity if item.unit_price is not None else None
    resp.is_fulfilled = item.fulfilled_quantity >= item.quantity
    return resp


@router.patch("/{order_id}/items/{item_id}", response_model=OrderItemResponse)
def update_order_item(order_id: int, item_id: int, data: OrderItemUpdate, current_user: dict = Depends(require_role("admin", scope="write")), db: Session = Depends(get_db)):
    """Update an order line item."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not check_org_access(current_user, order.org_id):
        raise HTTPException(status_code=404, detail="Order not found")
    _fulfillment().lock_orders(db,[order_id])
    if "quantity" in data.model_fields_set:
        _fulfillment().assert_order_structure_edit_allowed(db,order_id)

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
    resp.subtotal = item.unit_price * item.quantity if item.unit_price is not None else None
    resp.is_fulfilled = item.fulfilled_quantity >= item.quantity
    return resp


@router.delete("/{order_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_order_item(order_id: int, item_id: int, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Remove a line item from an order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not check_org_access(current_user, order.org_id):
        raise HTTPException(status_code=404, detail="Order not found")
    _fulfillment().assert_order_structure_edit_allowed(db,order_id)

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
def schedule_order(order_id: int, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Generate jobs for an order based on BOM."""
    order = db.query(Order).options(selectinload(Order.items)).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not check_org_access(current_user, order.org_id):
        raise HTTPException(status_code=404, detail="Order not found")

    provider=_fulfillment()
    provider.lock_orders(db,[order_id])
    db.expire_all()
    order=db.query(Order).options(
        joinedload(Order.items).joinedload(OrderItem.product).joinedload(Product.components).joinedload(ProductComponent.model),
        joinedload(Order.items).joinedload(OrderItem.product).joinedload(Product.consumable_links).joinedload(ProductConsumable.consumable),
    ).filter(Order.id==order_id).populate_existing().first()
    if not order or not check_org_access(current_user, order.org_id):
        raise HTTPException(status_code=404, detail="Order not found")
    if order.status != OrderStatus.PENDING:
        raise HTTPException(status_code=409, detail="Only a pending order can generate production")
    historical_jobs = db.query(Job.id).join(OrderItem).filter(OrderItem.order_id == order.id).first()
    historical_usage = db.query(ConsumableUsage.id).filter(ConsumableUsage.order_id == order.id).first()
    if historical_jobs or historical_usage:
        raise HTTPException(status_code=409, detail="This order already has production history. Review its existing jobs and usage before creating replacement work.")

    # Resolve the complete authorized graph and demand before any writes.
    planned_jobs = []
    demand = {}
    requirements = {}
    for item in order.items:
        product = item.product
        if not product or not check_org_access(current_user, product.org_id):
            raise HTTPException(status_code=404, detail="Product not found")
        if item.quantity <= 0:
            raise HTTPException(status_code=409, detail="Order quantities must be positive")
        requirements[item.id]={}
        for component in product.components:
            model = component.model
            if not model or not check_org_access(current_user, model.org_id):
                raise HTTPException(status_code=404, detail="Model not found")
            if component.quantity_needed <= 0:
                raise HTTPException(status_code=409, detail="BOM quantities must be positive")
            requirements[item.id][model.id]=requirements[item.id].get(model.id,0)+component.quantity_needed
            pieces_needed = item.quantity * component.quantity_needed
            pieces_per_job = model.quantity_per_bed if model.quantity_per_bed is not None else 1
            if pieces_per_job <= 0:
                raise HTTPException(status_code=409, detail="Model bed quantity must be positive")
            jobs_needed = -(-pieces_needed // pieces_per_job)
            for index in range(jobs_needed):
                quantity = min(pieces_per_job, pieces_needed - index * pieces_per_job)
                planned_jobs.append((item, model, quantity))
        for link in product.consumable_links:
            consumable = link.consumable
            if not consumable:
                raise HTTPException(status_code=404, detail="Consumable not found")
            if consumable.status != "active":
                raise HTTPException(status_code=409, detail="A required consumable is inactive")
            if link.quantity_per_product is None or not math.isfinite(link.quantity_per_product):
                raise HTTPException(status_code=409, detail="Consumable BOM quantity must be finite")
            needed = link.quantity_per_product * item.quantity
            if not math.isfinite(needed) or needed <= 0:
                raise HTTPException(status_code=409, detail="Consumable demand must be finite and positive")
            previous = demand.get(consumable.id, (consumable, 0))[1]
            total = previous + needed
            if not math.isfinite(total):
                raise HTTPException(status_code=409, detail="Consumable demand is too large")
            demand[consumable.id] = (consumable, total)

    if not planned_jobs and not demand:
        db.refresh(order)
        if order.status != OrderStatus.PENDING:
            raise HTTPException(status_code=409, detail="Order state changed; review the order before scheduling")
        return {"success": True, "order_id": order_id, "jobs_created": 0,
                "details": [], "consumables_deducted": []}

    jobs_created = []
    consumable_deductions = []
    try:
        # This conditional write serializes competing sessions on both providers.
        claimed = db.execute(update(Order).where(
            Order.id == order.id, Order.status == OrderStatus.PENDING
        ).values(status=OrderStatus.IN_PROGRESS).execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise HTTPException(status_code=409, detail="This order was already scheduled or its state changed")

        # Successful arithmetic updates record exactly the units actually consumed.
        for consumable_id in sorted(demand):
            consumable, needed = demand[consumable_id]
            remaining = db.execute(update(Consumable).where(
                Consumable.id == consumable_id, Consumable.status == "active",
                Consumable.current_stock >= needed
            ).values(current_stock=Consumable.current_stock - needed,
                     updated_at=datetime.now(timezone.utc)).returning(
                Consumable.current_stock).execution_options(synchronize_session=False)).scalar_one_or_none()
            if remaining is None:
                raise HTTPException(status_code=409, detail="Insufficient active consumable stock; restock and try again")
            if not math.isfinite(remaining):
                raise HTTPException(status_code=409, detail="Stored consumable stock is invalid; correct it before scheduling")
            db.add(ConsumableUsage(consumable_id=consumable_id, order_id=order.id,
                quantity_used=needed, notes=f"Auto-deducted for Order #{order.order_number or order.id}"))
            consumable_deductions.append({"consumable": consumable.name,
                "quantity_deducted": needed, "remaining_stock": remaining})

        for item in order.items:
            provider.record_item(db,item,requirements[item.id],bool(item.product.consumable_links))
        for item, model, quantity in planned_jobs:
            job=Job(model_id=model.id,
                item_name=f"{model.name} (Order #{order.order_number or order.id})",
                quantity=1, order_item_id=item.id, quantity_on_bed=quantity,
                status=JobStatus.PENDING, duration_hours=model.build_time_hours,
                filament_type=model.default_filament_type,
                charged_to_org_id=order.org_id, charged_to_user_id=current_user.get("id"),
                submitted_by=current_user.get("id"))
            _stage_prospective_order_resource(db, job, "job", current_user, order.org_id)
            provider.bind_job(db,job)
            jobs_created.append({"model": model.name, "quantity_on_bed": quantity})
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"success": True, "order_id": order_id, "jobs_created": len(jobs_created),
            "details": jobs_created, "consumables_deducted": consumable_deductions}


@router.get("/{order_id}/invoice.pdf")
def get_order_invoice(
    order_id: int,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db)
):
    """Generate a branded PDF invoice for an order."""
    from modules.orders.invoice_generator import InvoiceGenerator, InvoiceUnsupportedText, InvoiceInvalidAmount, InvoiceFooterTooLarge
    order = db.query(Order).options(joinedload(Order.items)).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not check_org_access(current_user, order.org_id):
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        from modules.organizations.branding import get_or_create_branding, branding_to_dict
        enriched = _enrich_order_response(order, db)
        branding = branding_to_dict(get_or_create_branding(db))

        gen = InvoiceGenerator(branding, enriched.model_dump())
        pdf_bytes = bytes(gen.generate())
    except InvoiceUnsupportedText:
        raise HTTPException(status_code=422, detail="Invoice text contains characters outside the supported fonts.")
    except InvoiceInvalidAmount:
        raise HTTPException(status_code=409, detail="Invoice contains invalid billing amounts.")
    except InvoiceFooterTooLarge:
        raise HTTPException(status_code=422, detail="Branding footer is too long for the invoice. Shorten it before downloading.")
    except Exception as e:
        log.error("PDF generation failed for order %s (%s)", order_id, type(e).__name__)
        raise HTTPException(status_code=500, detail="PDF generation failed. Check server logs for details.")

    return FastAPIResponse(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="invoice_{order.id}.pdf"',
            "Content-Length": str(len(pdf_bytes)),
        }
    )


@router.patch("/{order_id}/ship", response_model=OrderResponse)
def ship_order(order_id: int, data: OrderShipRequest, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Mark an order as shipped."""
    order = db.query(Order).options(selectinload(Order.items)).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not check_org_access(current_user, order.org_id):
        raise HTTPException(status_code=404, detail="Order not found")

    order = _locked_order(db, order_id, current_user)
    order.status = OrderStatus.SHIPPED
    order.tracking_number = data.tracking_number
    order.shipped_date = data.shipped_date or datetime.now(timezone.utc)

    db.commit()
    db.refresh(order)
    return _enrich_order_response(order, db)


@router.post("/{order_id}/items/{item_id}/confirm-production", response_model=OrderResponse)
def confirm_order_item_production(order_id: int, item_id: int, data: OrderProductionConfirmation,
                                  current_user: dict = Depends(require_role("operator", scope="write")),
                                  db: Session = Depends(get_db)):
    """Explicitly record production without reconstructing historical requirements."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order or not check_org_access(current_user, order.org_id):
        raise HTTPException(404, "Order not found")
    order = _locked_order(db, order_id, current_user)
    changed = _fulfillment().confirm_item(db, order_id, item_id, data.quantity)
    if changed:
        log_audit(db, 'order.production_confirmed', 'order_item', item_id,
                  {'order_id': order_id, 'quantity': data.quantity, 'actor_id': current_user['id']})
    db.commit()
    db.expire_all()
    order = db.query(Order).options(selectinload(Order.items)).filter(Order.id == order_id).first()
    return _enrich_order_response(order, db)
