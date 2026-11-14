"""Cost per print table (Pro): averages by model and product, against hand-computed figures.

Pricing and spool prices are those of test_cost_actual.py (machine 0.25 per hour, from settings
5.25 per job, spool 71 at 0.02 per gram). Model 101 "Bracket" has five completed jobs:
  801  2 pieces  actual 8.40 (0.45 of it on a failed attempt)   estimate 7.50
  811  1 piece   50 g + 2 h: 1.00 + 0.50 + 5.25 = 6.75           estimate 7.50
  812  1 piece   100 g + 1 h: 2.00 + 0.25 + 5.25 = 7.50          estimate 6.00
  805  partial (spool had no price), 802 finished by hand (no usage): in no average
  actual 22.65 over 4 pieces = 5.6625 -> 5.66; estimate 21.00 over 4 pieces = 5.25
  variance (22.65 - 21.00) / 21.00 = 7.857 % -> 7.9; failed share 0.45 / 22.65 = 1.987 % -> 2.0
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_cost_actual import add_attempt, allocation, cost_db, snapshot, used  # noqa: F401

BRACKET = {"id": 101, "name": "Bracket", "jobs": 5, "jobs_with_full_cost": 3, "jobs_partial": 1, "jobs_without_usage": 1,
           "pieces": 4, "average_actual_per_piece": 5.66, "average_estimate_per_piece": 5.25,
           "variance_percent": 7.9, "failure_share_percent": 2.0}


@pytest.fixture
def table_db(cost_db):
    cost_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(101,'Bracket',1),(102,'Foreign part',2)"))
    cost_db.execute(text("INSERT INTO jobs(id,item_name,status,quantity,quantity_on_bed,estimated_cost,charged_to_org_id,model_id,actual_end) VALUES"
                         "(811,'Second','completed',1,1,7.50,1,101,'2026-01-06 12:00:00'),"
                         "(812,'Third','completed',1,1,6.00,1,101,'2026-01-07 12:00:00')"))
    cost_db.execute(text("UPDATE jobs SET model_id=101,actual_end='2026-01-05 13:00:00' WHERE id IN (801,802,805)"))
    cost_db.execute(text("UPDATE jobs SET model_id=102 WHERE id=803"))
    add_attempt(cost_db, 9011, 811, "completed", 7200, snapshot(allocation(71, "20", "1000")), [used(71, 50.0)])
    add_attempt(cost_db, 9012, 812, "completed", 3600, snapshot(allocation(71, "20", "1000")), [used(71, 100.0)])
    cost_db.commit()
    return cost_db


@pytest.fixture
def table_client(table_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.reporting.routes.cost_per_print import router
    app = FastAPI()
    app.include_router(router)

    def request_db():
        with Session(table_db.get_bind()) as session:
            yield session
    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: {"id": 3, "role": "operator", "group_id": 1, "is_active": True}
    return TestClient(app)


def rows(client, **params):
    response = client.get("/analytics/cost-per-print", params=params)
    assert response.status_code == 200, response.text
    return {row["id"]: row for row in response.json()["rows"]}


def test_model_row_matches_the_hand_computed_averages(table_client, pro_license):
    body = table_client.get("/analytics/cost-per-print").json()
    assert body["configured"] is True and body["group"] == "model" and body["minimum_jobs"] == 3
    found = {row["id"]: row for row in body["rows"]}
    assert found[101] == BRACKET
    # Job 806 has no model: one complete job is not enough for an average.
    assert found[None] == {"id": None, "name": None, "jobs": 1, "jobs_with_full_cost": 1, "jobs_partial": 0,
                           "jobs_without_usage": 0, "pieces": 1, "average_actual_per_piece": None,
                           "average_estimate_per_piece": None, "variance_percent": None, "failure_share_percent": None}
    assert [row["id"] for row in body["rows"]] == [101, None]


def test_averages_need_three_complete_jobs(table_client, table_db, pro_license):
    table_db.execute(text("UPDATE jobs SET status='failed' WHERE id=812"))
    table_db.commit()
    row = rows(table_client)[101]
    assert (row["jobs"], row["jobs_with_full_cost"], row["pieces"]) == (4, 2, 3)
    assert all(row[key] is None for key in ("average_actual_per_piece", "average_estimate_per_piece",
                                            "variance_percent", "failure_share_percent"))


def test_variance_is_negative_when_prints_cost_less_than_estimated(table_client, table_db, pro_license):
    table_db.execute(text("UPDATE jobs SET estimated_cost=10 WHERE id IN (801,811,812)"))
    table_db.commit()
    row = rows(table_client)[101]
    # estimate 30.00 over 4 pieces = 7.50; (22.65 - 30.00) / 30.00 = -24.5 %
    assert row["average_estimate_per_piece"] == 7.50 and row["variance_percent"] == -24.5
    assert row["average_actual_per_piece"] == 5.66


def test_a_job_without_an_estimate_keeps_the_actual_average_but_gives_no_variance(table_client, table_db, pro_license):
    table_db.execute(text("UPDATE jobs SET estimated_cost=NULL WHERE id=812"))
    table_db.commit()
    row = rows(table_client)[101]
    assert row["average_actual_per_piece"] == 5.66
    assert row["average_estimate_per_piece"] is None and row["variance_percent"] is None


def test_a_job_with_an_unknown_piece_count_is_not_averaged(table_client, table_db, pro_license):
    table_db.execute(text("UPDATE jobs SET quantity_on_bed=NULL WHERE id=812"))
    table_db.commit()
    row = rows(table_client)[101]
    assert row["jobs_with_full_cost"] == 2 and row["average_actual_per_piece"] is None


def test_date_window_uses_the_day_the_job_ended(table_client, pro_license):
    row = rows(table_client, **{"from": "2026-01-06", "to": "2026-01-07"})[101]
    assert (row["jobs"], row["jobs_with_full_cost"]) == (2, 2)
    assert rows(table_client, **{"from": "2026-01-08"}).get(101) is None
    assert table_client.get("/analytics/cost-per-print", params={"from": "yesterday"}).status_code == 422


def test_other_tenants_jobs_and_models_are_not_in_the_table(table_client, pro_license):
    found = rows(table_client)
    assert 102 not in found and "Foreign part" not in str(found)
    assert sum(row["jobs"] for row in found.values()) == 6  # 801, 802, 805, 806, 811, 812; not 803


def test_education_submissions_are_left_out(table_client, table_db, pro_license):
    # review_db job 13 is an Education submission of model 12; finish it with a full cost record.
    table_db.execute(text("UPDATE jobs SET status='completed',quantity_on_bed=1,estimated_cost=1 WHERE id=13"))
    add_attempt(table_db, 9013, 13, "completed", 3600, snapshot(allocation(71, "20", "1000")), [used(71, 5.0)])
    table_db.commit()
    found = rows(table_client)
    assert 12 not in found and sum(row["jobs"] for row in found.values()) == 6


def test_controlled_jobs_follow_the_callers_audience(table_client, table_db, pro_license):
    table_db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'secure')"))
    table_db.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                          "VALUES(811,'CUI',1,'Secure',7,'fixture',CURRENT_TIMESTAMP)"))
    table_db.commit()
    row = rows(table_client)[101]
    assert (row["jobs"], row["jobs_with_full_cost"]) == (4, 2) and row["average_actual_per_piece"] is None
    table_db.execute(text("INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,3)"))
    table_db.commit()
    assert rows(table_client)[101] == BRACKET


def test_group_by_product_follows_the_order_line(table_client, table_db, pro_license):
    table_db.execute(text("INSERT INTO products(id,name,org_id,price) VALUES(201,'Bracket kit',1,20),(202,'Foreign kit',2,30)"))
    table_db.execute(text("INSERT INTO orders(id,org_id,status) VALUES(301,1,'pending'),(302,2,'pending')"))
    table_db.execute(text("INSERT INTO order_items(id,order_id,product_id,quantity) VALUES(401,301,201,4),(402,302,202,1)"))
    table_db.execute(text("UPDATE jobs SET order_item_id=401 WHERE id IN (801,811,812)"))
    table_db.execute(text("UPDATE jobs SET order_item_id=402 WHERE id=805"))
    table_db.commit()
    found = rows(table_client, group="product")
    assert found[201] == dict(BRACKET, id=201, name="Bracket kit", jobs=3, jobs_partial=0, jobs_without_usage=0)
    # A product of another organisation is never named; its job falls in the unnamed row.
    assert 202 not in found and found[None]["jobs"] == 3 and "Foreign kit" not in str(found)


def test_hidden_until_pricing_is_configured(table_client, table_db, pro_license):
    table_db.execute(text("DELETE FROM system_config WHERE key='pricing_config'"))
    table_db.commit()
    body = table_client.get("/analytics/cost-per-print").json()
    assert body["configured"] is False and body["rows"] == []


def test_hidden_when_only_the_interface_mode_was_saved(table_client, table_db, pro_license):
    """Switching interface mode creates the settings row marked 'pricing not saved': still hidden."""
    table_db.execute(text("UPDATE system_config SET value=:value WHERE key='pricing_config'"),
                     {"value": json.dumps({"ui_mode": "simple", "pricing_saved": False})})
    table_db.commit()
    body = table_client.get("/analytics/cost-per-print").json()
    assert body["configured"] is False and body["rows"] == []


def test_the_saved_marker_decides_and_is_not_handed_to_the_pricing_rule(table_db):
    from modules.models_library.services import DEFAULT_PRICING_CONFIG, saved_pricing_config

    def stored(value):
        table_db.execute(text("UPDATE system_config SET value=:value WHERE key='pricing_config'"), {"value": json.dumps(value)})
        table_db.commit()
        table_db.expire_all()
        return saved_pricing_config(table_db)

    assert stored({"ui_mode": "simple", "pricing_saved": False}) == (dict(DEFAULT_PRICING_CONFIG), False)
    assert stored({"hourly_rate": 3.0, "pricing_saved": True}) == ({"hourly_rate": 3.0}, True)
    # A row saved before the marker existed counts as saved.
    assert stored({"hourly_rate": 3.0}) == ({"hourly_rate": 3.0}, True)


def test_table_and_export_need_a_paid_license_while_the_job_cost_stays_free(table_client, tier_license):
    tier_license(None)
    for path in ("/analytics/cost-per-print", "/export/cost-per-print"):
        response = table_client.get(path)
        assert response.status_code == 403 and response.headers["X-ODIN-License-Feature"] == "analytics"
    from modules.jobs.routes.cost import router
    table_client.app.include_router(router)
    assert table_client.get("/jobs/801/cost").json()["total"] == 8.40


def test_csv_export_follows_the_csv_gate_and_role(table_client, pro_license, monkeypatch):
    response = table_client.get("/export/cost-per-print")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0] == ("Model,Completed jobs,Jobs with full cost records,Pieces made,Average actual cost per piece,"
                        "Average estimate per piece,Actual vs estimate (%),Spent on failed attempts (%)")
    assert lines[1] == "Bracket,5,3,4,5.66,5.25,7.9,2.0" and lines[2] == "No model,1,1,1,,,,"
    assert not any(word in response.text.lower() for word in ("profit", "margin", "cogs"))
    from core.dependencies import get_current_user
    table_client.app.dependency_overrides[get_current_user] = lambda: {"id": 4, "role": "viewer", "group_id": 1, "is_active": True}
    assert table_client.get("/export/cost-per-print").status_code == 403
    assert table_client.get("/analytics/cost-per-print").status_code == 200
    table_client.app.dependency_overrides[get_current_user] = lambda: {"id": 3, "role": "operator", "group_id": 1, "is_active": True}
    import license_manager
    real = license_manager.get_license()
    monkeypatch.setattr(real, "has_feature", lambda feature: feature != "csv_export")
    monkeypatch.setattr(license_manager, "get_license", lambda: real)
    refused = table_client.get("/export/cost-per-print")
    assert refused.status_code == 403 and refused.headers["X-ODIN-License-Feature"] == "csv_export"


def test_statement_count_does_not_grow_with_the_number_of_jobs(table_client, table_db, pro_license):
    statements = []
    event.listen(table_db.get_bind(), "before_cursor_execute", lambda *args: statements.append(args[2]))
    assert table_client.get("/analytics/cost-per-print").status_code == 200
    before = len(statements)
    for identifier in range(820, 850):
        table_db.execute(text("INSERT INTO jobs(id,item_name,status,quantity,quantity_on_bed,estimated_cost,charged_to_org_id,model_id) "
                              "VALUES(:id,'Bulk','completed',1,1,7,1,101)"), {"id": identifier})
        add_attempt(table_db, identifier + 9100, identifier, "completed", 3600, snapshot(allocation(71, "20", "1000")), [used(71, 5.0)])
    table_db.commit()
    statements.clear()
    body = table_client.get("/analytics/cost-per-print").json()
    assert next(row for row in body["rows"] if row["id"] == 101)["jobs"] == 35
    assert len(statements) == before
