"""Ranked print-next list and held-job queueing. Square is read only, never written."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from core.db import get_db
from core.private_routes import PrivateRoute
from core.rbac import require_action
from modules.orders.replenishment_queue import QueueRequest


class ReplenishmentRoute(PrivateRoute):
    invalid_detail = 'Invalid queue request. Choose a product, its suggested plate and a whole number of plates.'


router = APIRouter(prefix='/production', tags=['Print next'], route_class=ReplenishmentRoute)
# An interactive session, or a token with the read scope: the list changes nothing.
reader = require_action('products.assortment_preview', scope='read')
# One permission key per route (core.rbac_routes); the right to create jobs is also checked in queue().
queuer = require_action('products.assortment_preview', scope='write:jobs')


@router.get('/replenishment')
def print_next(cached: bool = Query(default=False), user=Depends(reader), db: Session = Depends(get_db)):
    """``cached=true`` never contacts Square: it answers from the last read or recorded snapshot."""
    from modules.orders.replenishment import listing
    return listing(db, user, remote=not cached)


@router.post('/replenishment/queue')
def queue_plates(data: QueueRequest, user=Depends(queuer), db: Session = Depends(get_db)):
    from modules.orders.replenishment_queue import queue
    return queue(db, user, data)
