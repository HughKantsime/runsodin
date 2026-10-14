"""Claim F09-9.2-02: "Schedule All" generates BOM jobs, assigns printers with color-match
scoring, and deducts consumable stock.

Drives the real POST /orders/{id}/schedule route (the only order "schedule" action the
frontend calls) on an owned SQLite store, inspects persisted jobs/stock, then runs the
real scheduler to see where printer assignment and color scoring actually happen.
"""
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_order_production_behavior import production_order

ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = ROOT / 'artifacts' / 'application-certification' / 'claims-protocols-backend'


def _jobs(db, order_id):
    with Session(db.get_bind()) as fresh:
        return [dict(r) for r in fresh.execute(text(
            'SELECT id,printer_id,status,match_score,quantity_on_bed FROM jobs WHERE order_item_id IN '
            '(SELECT id FROM order_items WHERE order_id=:o) ORDER BY id'), {'o': order_id}).mappings()]


def test_schedule_all_generates_and_deducts_but_assignment_needs_scheduler_run(production_order, review_db):
    client, identity, order_id = production_order
    # Model requires red; printer 10 has red loaded, printer 9 white.
    review_db.execute(text("UPDATE models SET color_requirements=:c WHERE id=101"), {'c': json.dumps({'1': {'color': 'red'}})})
    review_db.execute(text("UPDATE filament_slots SET color='white' WHERE printer_id=9"))
    review_db.execute(text("UPDATE filament_slots SET color='red' WHERE printer_id=10"))
    review_db.commit()
    response = client.post(f'/orders/{order_id}/schedule')
    assert response.status_code == 200, response.text
    body = response.json()
    after_schedule = _jobs(review_db, order_id)
    with Session(review_db.get_bind()) as fresh:
        stock = fresh.execute(text('SELECT current_stock FROM consumables WHERE id=101')).scalar_one()
    record = {'schedule_response': body, 'jobs_after_schedule': after_schedule, 'stock_after_schedule': stock}
    # BOM generation + stock deduction happen in the Schedule action.
    assert body['jobs_created'] == 3 and [j['quantity_on_bed'] for j in after_schedule] == [4, 4, 1]
    assert stock == 14 and body['consumables_deducted'][0]['quantity_deducted'] == 6
    # No printer assignment / color score is produced by the Schedule action itself.
    assert all(j['printer_id'] is None and j['match_score'] is None for j in after_schedule)
    # A separate scheduler run performs color-scored assignment.
    from modules.jobs.scheduler import run_scheduler
    with Session(review_db.get_bind()) as db:
        result = run_scheduler(db)
        db.commit()
    after_run = _jobs(review_db, order_id)
    record['scheduler_scheduled'] = result.scheduled_count
    record['jobs_after_scheduler_run'] = after_run
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    (RECEIPTS / 'schedule-all.json').write_text(json.dumps(record, indent=1, default=str))
    assigned = [j for j in after_run if j['printer_id'] is not None]
    assert assigned, 'scheduler did not assign any order job'
    assert {j['printer_id'] for j in assigned} == {10}
    assert {j['match_score'] for j in assigned} == {100}
