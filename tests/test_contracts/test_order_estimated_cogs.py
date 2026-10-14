"""Per-order BOM-estimated COGS, profit and margin (F09-9.3-02 / F09-9.1-04 decision: labeled estimates)."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_product_bom_integrity import bom_client, review_db
from tests.test_contracts.test_product_mixed_bom_cost_behavior import test_mixed_bom_estimate_accounts_for_every_component_cost as build_bom


def _order(db,revenue=60.0,fees=(3.0,1.5,4.5),quantity=2,identifier=501):
    db.execute(text("INSERT INTO orders(id,order_number,status,revenue,platform_fees,payment_fees,shipping_cost,org_id) "
                    "VALUES(:id,:num,'pending',:rev,:pf,:pay,:ship,1)"),
               {'id':identifier,'num':f'EST-{identifier}','rev':revenue,'pf':fees[0],'pay':fees[1],'ship':fees[2]})
    db.execute(text("INSERT INTO order_items(order_id,product_id,quantity,unit_price,fulfilled_quantity) VALUES(:o,101,:q,30,0)"),{'o':identifier,'q':quantity})
    db.commit()


def _views(client,identifier=501):
    detail=client.get(f'/orders/{identifier}');assert detail.status_code==200,detail.text
    listing=client.get('/orders');assert listing.status_code==200,listing.text
    return detail.json(),next(o for o in listing.json() if o['id']==identifier)


def test_known_bom_order_reports_exact_estimates_in_detail_and_list(bom_client,review_db):
    build_bom(bom_client,review_db,'mixed_known',10.5,'detail')   # 10.50 per unit
    _order(review_db)
    for view in _views(bom_client):
        assert view['estimated_cogs']==21.0           # 2 × 10.50 filament + consumables
        assert view['estimated_profit']==30.0         # 60 − 21 − (3 + 1.5 + 4.5)
        assert view['estimated_margin_percent']==50.0
    detail,_=_views(bom_client)
    assert detail['actual_cost'] is None and detail['profit'] is None and detail['margin_percent'] is None


def test_unknown_component_cost_makes_every_estimate_unknown(bom_client,review_db):
    build_bom(bom_client,review_db,'unknown_consumable',None,'detail')
    _order(review_db)
    for view in _views(bom_client):
        assert view['estimated_cogs'] is None and view['estimated_profit'] is None and view['estimated_margin_percent'] is None


def test_missing_revenue_keeps_cogs_but_not_profit(bom_client,review_db):
    build_bom(bom_client,review_db,'mixed_known',10.5,'detail')
    _order(review_db,revenue=None)
    for view in _views(bom_client):
        assert view['estimated_cogs']==21.0 and view['estimated_profit'] is None and view['estimated_margin_percent'] is None


def test_zero_revenue_has_profit_but_no_margin_and_loss_is_negative(bom_client,review_db):
    build_bom(bom_client,review_db,'mixed_known',10.5,'detail')
    _order(review_db,revenue=0.0,fees=(None,None,None))
    detail,summary=_views(bom_client)
    assert detail['estimated_profit']==-21.0 and detail['estimated_margin_percent'] is None
    _order(review_db,revenue=20.0,fees=(None,None,None),identifier=502)
    detail,_=_views(bom_client,502)
    assert detail['estimated_profit']==-1.0 and detail['estimated_margin_percent']==-5.0


def test_order_without_items_has_no_estimate(bom_client,review_db):
    review_db.execute(text("INSERT INTO orders(id,order_number,status,revenue,org_id) VALUES(503,'EMPTY','pending',10,1)"));review_db.commit()
    detail,summary=_views(bom_client,503)
    assert detail['estimated_cogs'] is None and summary['estimated_cogs'] is None
