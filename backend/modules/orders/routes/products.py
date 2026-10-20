"""O.D.I.N. — Products and BOM Components."""

from core.rbac import should_filter_org, require_action

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import update
from typing import List
import logging

from core.db import get_db
from core.rbac import require_role, get_org_scope, check_org_access
from modules.orders.models import Product, ProductComponent
from modules.orders.product_costing import estimated_product_cogs
from modules.inventory.models import Consumable, ProductConsumable
from modules.models_library.models import Model
from modules.orders.schemas import (
    ProductResponse, ProductCreate, ProductUpdate,
    ProductComponentResponse, ProductComponentCreate,
    ProductConsumableResponse, ProductConsumableCreate,
)

log = logging.getLogger("odin.api")

router = APIRouter(prefix="/products", tags=["Products"])


@router.get("", response_model=List[ProductResponse])
def list_products(current_user: dict = Depends(require_role("viewer", scope="read")), db: Session = Depends(get_db)):
    """List all products."""
    org = get_org_scope(current_user)
    query = db.query(Product)
    if should_filter_org(current_user, org):
        query = query.filter((Product.org_id == org) | (Product.org_id == None))
    products = query.all()
    result = []
    for p in products:
        resp = ProductResponse.model_validate(p)
        resp.component_count = len(p.components)
        resp.estimated_cogs = estimated_product_cogs(p)
        # Enrich consumables
        resp.consumables = [
            ProductConsumableResponse(id=pc.id, product_id=pc.product_id, consumable_id=pc.consumable_id,
                                      quantity_per_product=pc.quantity_per_product, notes=pc.notes,
                                      consumable_name=pc.consumable.name if pc.consumable else None)
            for pc in getattr(p, 'consumable_links', [])
        ]
        result.append(resp)
    return result


@router.post("", response_model=ProductResponse)
def create_product(data: ProductCreate, current_user: dict = Depends(require_action("products.create", scope="write")), db: Session = Depends(get_db)):
    """Create a new product with optional BOM components."""
    # Validate the complete supplied BOM before persisting the parent or any child.
    for component in data.components or []:
        model = db.query(Model).filter(Model.id == component.model_id).first()
        if not model or not check_org_access(current_user, model.org_id):
            raise HTTPException(status_code=404, detail="Model not found")
    product = Product(
        name=data.name,
        sku=data.sku,
        price=data.price,
        description=data.description,
        org_id=current_user.get("group_id"),
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


@router.get("/{product_id}", response_model=ProductResponse)
def get_product(product_id: int, current_user: dict = Depends(require_role("viewer", scope="read")), db: Session = Depends(get_db)):
    """Get a product with its BOM components."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if not check_org_access(current_user, product.org_id):
        raise HTTPException(status_code=404, detail="Product not found")

    resp = ProductResponse.model_validate(product)
    resp.component_count = len(product.components)

    # Enrich components with model names
    enriched_components = []
    for comp in product.components:
        comp_resp = ProductComponentResponse.model_validate(comp)
        if comp.model:
            comp_resp.model_name = comp.model.name
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

    resp.estimated_cogs = estimated_product_cogs(product)
    return resp


@router.patch("/{product_id}", response_model=ProductResponse)
def update_product(product_id: int, data: ProductUpdate, current_user: dict = Depends(require_action("products.edit", scope="write")), db: Session = Depends(get_db)):
    """Update a product."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if not check_org_access(current_user, product.org_id):
        raise HTTPException(status_code=404, detail="Product not found")

    from modules.orders.finishing import lock
    from modules.orders.square_store import own_product
    lock(db,product.org_id or 0)
    product=own_product(db,product.org_id or 0,product.id,lock=True)

    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(product, key, value)

    db.commit()
    db.refresh(product)
    return product


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(product_id: int, current_user: dict = Depends(require_action("products.delete", scope="write")), db: Session = Depends(get_db)):
    """Delete a product."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if not check_org_access(current_user, product.org_id):
        raise HTTPException(status_code=404, detail="Product not found")

    from modules.orders.square_store import forget_product
    forget_product(db, product)
    db.delete(product)
    db.commit()


# -------------- Product Components (BOM) --------------

@router.post("/{product_id}/components", response_model=ProductComponentResponse)
def add_product_component(product_id: int, data: ProductComponentCreate, current_user: dict = Depends(require_action("products.create", scope="write")), db: Session = Depends(get_db)):
    """Add a component to a product's BOM."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if not check_org_access(current_user, product.org_id):
        raise HTTPException(status_code=404, detail="Product not found")

    from modules.orders.finishing import lock
    from modules.orders.square_store import own_product
    lock(db,product.org_id or 0)
    product=own_product(db,product.org_id or 0,product.id,lock=True)

    model = db.query(Model).filter(Model.id == data.model_id).first()
    if not model or not check_org_access(current_user, model.org_id):
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


@router.delete("/{product_id}/components/{component_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_product_component(product_id: int, component_id: int, current_user: dict = Depends(require_action("products.edit", scope="write")), db: Session = Depends(get_db)):
    """Remove a component from a product's BOM."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if not check_org_access(current_user, product.org_id):
        raise HTTPException(status_code=404, detail="Product not found")

    from modules.orders.finishing import lock
    from modules.orders.square_store import own_product
    lock(db,product.org_id or 0)
    product=own_product(db,product.org_id or 0,product.id,lock=True)

    comp = db.query(ProductComponent).filter(
        ProductComponent.id == component_id,
        ProductComponent.product_id == product_id
    ).first()
    if not comp:
        raise HTTPException(status_code=404, detail="Component not found")

    db.delete(comp)
    db.commit()


# -------------- Product Consumables (BOM) --------------

@router.post("/{product_id}/consumables")
def add_product_consumable(product_id: int, data: ProductConsumableCreate,
                           current_user: dict = Depends(require_action("products.create", scope="write")), db: Session = Depends(get_db)):
    """Add a consumable to a product's BOM."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if not check_org_access(current_user, product.org_id):
        raise HTTPException(status_code=404, detail="Product not found")

    from modules.orders.finishing import lock
    from modules.orders.square_store import own_product
    lock(db,product.org_id or 0)
    product=own_product(db,product.org_id or 0,product.id,lock=True)
    if db.execute(update(Consumable).where(Consumable.id==data.consumable_id).values(id=Consumable.id)).rowcount!=1:
        raise HTTPException(404,'Consumable not found')
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


@router.delete("/{product_id}/consumables/{consumable_link_id}")
def remove_product_consumable(product_id: int, consumable_link_id: int,
                              current_user: dict = Depends(require_action("products.edit", scope="write")), db: Session = Depends(get_db)):
    """Remove a consumable from a product's BOM."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    if not check_org_access(current_user, product.org_id):
        raise HTTPException(status_code=404, detail="Product not found")

    from modules.orders.finishing import lock
    from modules.orders.square_store import own_product
    lock(db,product.org_id or 0)
    product=own_product(db,product.org_id or 0,product.id,lock=True)

    link = db.query(ProductConsumable).filter(
        ProductConsumable.id == consumable_link_id,
        ProductConsumable.product_id == product_id
    ).first()
    if not link:
        raise HTTPException(status_code=404, detail="Product consumable not found")
    db.delete(link)
    db.commit()
    return {"success": True}
