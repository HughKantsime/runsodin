"""The scheduler gives no new work to an Experimental printer until job sending is switched on."""
import json
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_order_production_behavior import production_order


def _assigned(db, order_id):
    with Session(db.get_bind()) as fresh:
        return {r[0] for r in fresh.execute(text(
            'SELECT printer_id FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:o) '
            'AND printer_id IS NOT NULL'), {'o': order_id})}


def _run(db):
    from modules.jobs.scheduler import run_scheduler
    with Session(db.get_bind()) as session:
        run_scheduler(session)
        session.commit()


def test_unopted_experimental_printer_gets_no_scheduled_work(production_order, review_db):
    from modules.printers import support_levels
    from modules.printers.models import Printer
    client, identity, order_id = production_order
    # Only printer 10 has the colour the model needs, and it is an OctoPrint (Experimental) printer.
    review_db.execute(text("UPDATE models SET color_requirements=:c WHERE id=101"), {'c': json.dumps({'1': {'color': 'red'}})})
    review_db.execute(text("UPDATE filament_slots SET color='white' WHERE printer_id=9"))
    review_db.execute(text("UPDATE filament_slots SET color='red' WHERE printer_id=10"))
    review_db.execute(text("UPDATE printers SET api_type='octoprint' WHERE id=10"))
    review_db.commit()
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    _run(review_db)
    assert 10 not in _assigned(review_db, order_id)

    with Session(review_db.get_bind()) as session:
        support_levels.set_experimental_enabled(session, session.get(Printer, 10), True, 3)
        session.commit()
    review_db.execute(text("UPDATE jobs SET printer_id=NULL, status='pending', scheduled_start=NULL, scheduled_end=NULL, match_score=NULL "
                           "WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:o)"), {'o': order_id})
    review_db.commit()
    _run(review_db)
    assert 10 in _assigned(review_db, order_id)
