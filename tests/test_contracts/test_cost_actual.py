"""Cost per print: the actual cost of a finished job, against hand-computed figures.

Pricing settings used throughout (PRICING):
  machine per hour   250 W / 1000 x 0.20 + 1000 / 5000            = 0.05 + 0.20 = 0.25
  labour             (5 + 5 + 0 + 10 plate) min / 60 x 12          = 4.00
  packaging 0.50, other 0.25, overhead 100 / 200                   = 0.50
  from settings      4.00 + 0.50 + 0.25 + 0.50                     = 5.25
  failure allowance  10 % (must never be added to the actual)
Spool 71: 20.00 for 1000 g = 0.02 per gram. Spool 72: 30.00 for 750 g = 0.04 per gram.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

PRICING = {
    "spool_cost": 25.0, "spool_weight": 1000.0, "hourly_rate": 12.0, "electricity_rate": 0.20,
    "printer_wattage": 250, "printer_cost": 1000.0, "printer_lifespan": 5000, "packaging_cost": 0.50,
    "failure_rate": 10.0, "monthly_rent": 100.0, "parts_per_month": 200, "post_processing_min": 5,
    "packing_min": 5, "support_min": 0, "plate_handling_min": 10, "default_margin": 50.0, "other_costs": 0.25,
}
SPOOL_A = {"spool_id": 71, "price": 20.0, "net_weight_g": 1000.0, "provenance": "measured", "reason": None}
SPOOL_B = {"spool_id": 72, "price": 30.0, "net_weight_g": 750.0, "provenance": "measured", "reason": None}


def attempt(identifier, outcome, hours, *usage):
    return {"id": identifier, "outcome": outcome, "hours": hours, "usage": list(usage) if usage else None}


def cost(attempts, config=PRICING):
    from modules.jobs.cost_actual import cost_actual
    return cost_actual(attempts, config)


# ---- the computation (pure) ----

def test_one_attempt_with_full_facts_gives_the_exact_total():
    result = cost([attempt(1, "completed", 2.0, dict(SPOOL_A, grams=50.0))])
    # material 50 x 0.02 = 1.00; machine 2 h x 0.25 = 0.50; from settings 5.25
    assert result["state"] == "complete" and result["unknown"] == []
    assert result["material"]["amount"] == 1.00
    assert result["machine"] == {"amount": 0.50, "complete": True, "hours": 2.0, "rate_per_hour": 0.25,
                                 "basis": "current_rates"}
    assert result["from_settings"] == {"amount": 5.25, "complete": True, "basis": "settings", "labor": 4.00,
                                       "packaging": 0.50, "other": 0.25, "overhead": 0.50}
    assert result["total"] == 6.75
    assert result["failed_attempts"] == {"amount": 0.0, "complete": True, "count": 0}


def test_two_spools_are_each_priced_at_their_own_frozen_price():
    result = cost([attempt(1, "completed", 2.0, dict(SPOOL_A, grams=50.0), dict(SPOOL_B, grams=30.0))])
    # 50 x 0.02 = 1.00 and 30 x 0.04 = 1.20
    assert result["material"]["spools"] == [
        {"spool_id": 71, "grams": 50.0, "amount": 1.00, "provenance": "measured"},
        {"spool_id": 72, "grams": 30.0, "amount": 1.20, "provenance": "measured"}]
    assert result["material"]["amount"] == 2.20
    assert result["total"] == 7.95


def test_failed_attempt_then_success_counts_both_and_adds_no_failure_allowance():
    result = cost([attempt(1, "failed", 1.0, dict(SPOOL_A, grams=10.0)),
                   attempt(2, "completed", 2.0, dict(SPOOL_A, grams=50.0))])
    # failed: 10 x 0.02 + 1 h x 0.25 = 0.45; success: 1.00 + 0.50; from settings 5.25 once
    assert result["state"] == "complete"
    assert result["failed_attempts"] == {"amount": 0.45, "complete": True, "count": 1}
    assert [(row["id"], row["outcome"], row["amount"]) for row in result["attempts"]] == [(1, "failed", 0.45), (2, "completed", 1.50)]
    assert result["material"]["spools"] == [{"spool_id": 71, "grams": 60.0, "amount": 1.20, "provenance": "measured"}]
    assert result["machine"]["hours"] == 3.0
    assert result["total"] == 7.20  # 1.20 + 0.75 + 5.25; a 10 % allowance would have made it more


def test_cancelled_attempt_counts_like_a_failed_one():
    result = cost([attempt(1, "cancelled", 1.0, dict(SPOOL_A, grams=10.0)),
                   attempt(2, "completed", 2.0, dict(SPOOL_A, grams=50.0))])
    assert result["failed_attempts"]["amount"] == 0.45 and result["total"] == 7.20


@pytest.mark.parametrize("reason", ["spool_price_missing", "spool_weight_missing", "price_not_recorded"])
def test_missing_spool_price_makes_the_total_partial_with_the_reason(reason):
    unpriced = {"spool_id": 71, "price": None, "net_weight_g": None, "provenance": "measured", "reason": reason}
    result = cost([attempt(1, "completed", 2.0, dict(unpriced, grams=50.0), dict(SPOOL_B, grams=30.0))])
    assert result["state"] == "partial"
    assert result["unknown"] == [{"part": "material", "reason": reason, "attempt_id": 1, "spool_id": 71}]
    assert result["material"]["complete"] is False
    assert result["material"]["spools"][0] == {"spool_id": 71, "grams": 50.0, "amount": None, "provenance": "measured"}
    # at least: spool 72 1.20 + machine 0.50 + from settings 5.25
    assert result["total"] == 6.95


def test_an_attempt_without_recorded_usage_is_unknown_never_estimated():
    result = cost([attempt(1, "completed", 2.0)])
    assert result["state"] == "partial"
    assert result["unknown"] == [{"part": "material", "reason": "usage_not_recorded", "attempt_id": 1, "spool_id": None}]
    assert result["total"] == 5.75  # machine 0.50 + from settings 5.25


def test_missing_print_time_makes_the_machine_line_partial():
    result = cost([attempt(1, "completed", None, dict(SPOOL_A, grams=50.0))])
    assert result["state"] == "partial"
    assert result["unknown"] == [{"part": "machine", "reason": "print_time_not_recorded", "attempt_id": 1, "spool_id": None}]
    assert result["machine"]["complete"] is False and result["machine"]["amount"] == 0.0
    assert result["total"] == 6.25  # material 1.00 + from settings 5.25


def test_an_unfinished_attempt_keeps_the_total_partial():
    result = cost([attempt(1, "open", 1.0, dict(SPOOL_A, grams=10.0))])
    assert result["state"] == "partial"
    assert result["unknown"] == [{"part": "attempt", "reason": "attempt_not_finished", "attempt_id": 1, "spool_id": None}]


def test_a_zero_price_spool_is_a_known_cost_of_zero():
    result = cost([attempt(1, "completed", 2.0, dict(SPOOL_A, price=0.0, grams=50.0))])
    assert result["state"] == "complete" and result["material"]["amount"] == 0.0 and result["total"] == 5.75


def test_job_without_attempts_has_no_actual():
    assert cost([]) == {"state": "none"}


def test_unusable_settings_are_unknown_not_zero():
    broken = {key: value for key, value in PRICING.items() if key != "printer_lifespan"}
    result = cost([attempt(1, "completed", 2.0, dict(SPOOL_A, grams=50.0))], broken)
    assert result["state"] == "partial"
    assert {"part": "settings", "reason": "settings_incomplete", "attempt_id": None, "spool_id": None} in result["unknown"]
    assert result["total"] == 1.00 and result["machine"]["complete"] is False and result["from_settings"]["amount"] is None


def test_from_settings_lines_match_the_estimate_rule_for_one_plate():
    """Estimate and actual differ only by material, hours and the failure allowance."""
    from modules.models_library.pricing_rule import price_item
    priced = price_item(PRICING, material_cost=1.00, print_hours=2.0, units_per_plate=1)
    result = cost([attempt(1, "completed", 2.0, dict(SPOOL_A, grams=50.0))])
    assert round(priced["subtotal"] - priced["failure"], 2) == result["total"]


# ---- the loader and the endpoint ----

def snapshot(*allocations):
    return json.dumps({"version": 1, "material_plan": {"state": "complete", "allocations": list(allocations)}})


def allocation(spool_id, price, net, state="recorded", reason=None):
    return {"spool_id": spool_id, "estimated_grams": 55.0,
            "purchase_inputs": {"state": state, "reason": reason, "price": price, "net_weight_g": net}}


def add_attempt(db, identifier, job_id, status, seconds, snap, consumption, state="accounted"):
    db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,started_at,ended_at,status,scheduled_job_id,"
                    "print_duration_seconds,spool_snapshot) VALUES(:id,9,'fixture','2026-01-05 10:00:00',"
                    "'2026-01-05 13:00:00',:status,:job,:seconds,:snap)"),
               {"id": identifier, "status": status, "job": job_id, "seconds": seconds, "snap": snap})
    if consumption is not None:
        db.execute(text("INSERT INTO print_archives(job_id,print_job_id,printer_id,print_name,status,accounting_state,"
                        "consumption_json,org_id,ownership_state) VALUES(:job,:id,9,'fixture',:status,:state,:consumption,1,'known')"),
                   {"job": job_id, "id": identifier, "status": status, "state": state, "consumption": json.dumps(consumption)})


def used(spool_id, grams, provenance="measured"):
    return {"spool_id": spool_id, "grams_used": grams, "quantity_provenance": provenance, "inventory_shortfall_g": 0}


@pytest.fixture
def cost_db(review_db):
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(70,'Acme','Black PLA','PLA')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,price,initial_weight_g,remaining_weight_g,org_id) VALUES"
                           "(71,70,999,5,500,1),(72,70,999,5,500,1)"))
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('pricing_config',:value)"), {"value": json.dumps(PRICING)})
    review_db.execute(text(
        "INSERT INTO jobs(id,item_name,status,quantity,quantity_on_bed,estimated_cost,charged_to_org_id) VALUES"
        "(801,'Full facts','completed',1,2,7.50,1),(802,'Hand completed','completed',1,1,7.50,1),"
        "(803,'Other tenant','completed',1,1,7.50,2),(804,'Still queued','pending',1,1,7.50,1),"
        "(805,'No price','completed',1,1,7.50,1),(806,'Monitor ledger','completed',1,1,NULL,1)"))
    add_attempt(review_db, 9001, 801, "failed", 3600, snapshot(allocation(71, "20", "1000")), [used(71, 10.0)])
    add_attempt(review_db, 9002, 801, "completed", 7200, snapshot(allocation(71, "20", "1000"), allocation(72, "30", "750")),
                [used(71, 50.0), used(72, 30.0)])
    add_attempt(review_db, 9003, 803, "completed", 7200, snapshot(allocation(71, "20", "1000")), [used(71, 50.0)])
    add_attempt(review_db, 9005, 805, "completed", 7200, snapshot(allocation(71, None, "1000", "unresolved", "missing_price")),
                [used(71, 50.0)])
    # Accounted by the printer monitor: no list on the archive, the ledger row names the attempt.
    add_attempt(review_db, 9006, 806, "completed", None, snapshot(allocation(72, "30", "750")), [], "accounted")
    review_db.execute(text("INSERT INTO spool_usage(spool_id,job_id,print_job_id,weight_used_g,notes) VALUES"
                           "(72,806,9006,30,'auto:moonraker: observed'),(72,806,NULL,500,'manual: correction')"))
    review_db.commit()
    return review_db


@pytest.fixture
def cost_client(cost_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.jobs.routes.cost import router
    app = FastAPI()
    app.include_router(router)

    def request_db():
        with Session(cost_db.get_bind()) as session:
            yield session
    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: {"id": 3, "role": "operator", "group_id": 1, "is_active": True}
    return TestClient(app)


def test_endpoint_reports_estimate_actual_and_difference(cost_client):
    """No license fixture: the per-job cost is free on every tier."""
    body = cost_client.get("/jobs/801/cost").json()
    # failed 10 x 0.02 + 1 h x 0.25 = 0.45; success 1.00 + 1.20 + 0.50 = 2.70; from settings 8.50 (two items, one plate)
    assert body["configured"] is True and body["finished"] is True and body["state"] == "complete"
    assert body["total"] == 11.65 and body["estimate"] == 7.50 and body["difference"] == 4.15
    assert body["pieces"] == 2
    assert body["failed_attempts"] == {"amount": 0.45, "complete": True, "count": 1}
    assert body["material"]["spools"] == [
        {"spool_id": 71, "grams": 60.0, "amount": 1.20, "provenance": "measured", "name": "Acme Black PLA"},
        {"spool_id": 72, "grams": 30.0, "amount": 1.20, "provenance": "measured", "name": "Acme Black PLA"}]
    assert not any(key.startswith("_") for key in body)


def test_price_is_the_one_frozen_at_start_not_the_spool_today(cost_client, cost_db):
    # The spools now say 999 for 5 g; the frozen 20 for 1000 g still prices the print.
    assert cost_client.get("/jobs/801/cost").json()["material"]["amount"] == 2.40
    cost_db.execute(text("DELETE FROM spool_usage"))
    cost_db.execute(text("DELETE FROM spools WHERE id=71"))
    cost_db.commit()
    body = cost_client.get("/jobs/801/cost").json()
    assert body["total"] == 11.65 and body["material"]["spools"][0]["name"] is None


def test_missing_frozen_price_is_partial_and_has_no_difference(cost_client):
    body = cost_client.get("/jobs/805/cost").json()
    assert body["state"] == "partial" and body["difference"] is None
    assert body["unknown"] == [{"part": "material", "reason": "spool_price_missing", "attempt_id": 9005, "spool_id": 71}]
    assert body["total"] == 5.75  # at least: machine 0.50 + from settings 5.25


def test_hand_completed_job_has_no_actual(cost_client):
    body = cost_client.get("/jobs/802/cost").json()
    assert body["state"] == "none" and "total" not in body and body["estimate"] == 7.50 and body["difference"] is None


def test_monitor_accounted_attempt_is_read_from_the_ledger_and_falls_back_to_start_end_time(cost_client):
    body = cost_client.get("/jobs/806/cost").json()
    # 30 g x 0.04 = 1.20 (the manual 500 g row is not the print); 3 h start-to-end x 0.25 = 0.75
    assert body["state"] == "complete" and body["material"]["amount"] == 1.20 and body["machine"]["hours"] == 3.0
    assert body["total"] == 7.20 and body["estimate"] is None and body["difference"] is None


def test_unresolved_accounting_is_not_read_as_usage(cost_client, cost_db):
    cost_db.execute(text("UPDATE print_archives SET accounting_state='unresolved',"
                         "consumption_json='[{\"spool_id\": null, \"grams_used\": 50, \"reason\": \"no_bound_spool\"}]' "
                         "WHERE print_job_id=9005"))
    cost_db.commit()
    body = cost_client.get("/jobs/805/cost").json()
    assert body["unknown"] == [{"part": "material", "reason": "usage_not_recorded", "attempt_id": 9005, "spool_id": None}]


def test_hidden_until_pricing_is_configured(cost_client, cost_db):
    cost_db.execute(text("DELETE FROM system_config WHERE key='pricing_config'"))
    cost_db.commit()
    assert cost_client.get("/jobs/801/cost").json() == {"job_id": 801, "configured": False, "finished": True}


def test_unfinished_job_reports_no_amounts(cost_client):
    assert cost_client.get("/jobs/804/cost").json() == {"job_id": 804, "configured": True, "finished": False}


def test_other_tenants_job_and_missing_job_are_not_found(cost_client):
    assert cost_client.get("/jobs/803/cost").status_code == 404
    assert cost_client.get("/jobs/999999/cost").status_code == 404


def test_controlled_job_follows_the_job_visibility_rule(cost_client, cost_db):
    cost_db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'secure')"))
    cost_db.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                         "VALUES(801,'CUI',1,'Secure',7,'fixture',CURRENT_TIMESTAMP)"))
    cost_db.commit()
    response = cost_client.get("/jobs/801/cost")
    assert response.status_code == 403 and "total" not in response.text
    cost_db.execute(text("INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,3)"))
    cost_db.commit()
    assert cost_client.get("/jobs/801/cost").json()["total"] == 11.65


def test_requires_sign_in(cost_client):
    from core.dependencies import get_current_user
    cost_client.app.dependency_overrides[get_current_user] = lambda: None
    assert cost_client.get("/jobs/801/cost").status_code == 401


def test_loader_statement_count_does_not_grow_with_jobs(cost_db):
    from sqlalchemy import event
    from modules.jobs.cost_actual import load_attempts
    for identifier in range(820, 850):
        cost_db.execute(text("INSERT INTO jobs(id,item_name,status,quantity,charged_to_org_id) VALUES(:id,'Bulk','completed',1,1)"),
                        {"id": identifier})
        add_attempt(cost_db, identifier + 9100, identifier, "completed", 3600, snapshot(allocation(71, "20", "1000")), [used(71, 5.0)])
    cost_db.commit()
    statements = []
    engine = cost_db.get_bind()
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    user = {"id": 3, "role": "operator", "group_id": 1}
    few = load_attempts(cost_db, [801, 805], user)
    count_few = len(statements)
    many = load_attempts(cost_db, [801, 805, *range(820, 850)], user)
    assert count_few == 3 and len(statements) - count_few == 3
    assert len(few[801]) == 2 and len(many[849]) == 1 and many[849][0]["usage"][0]["grams"] == 5.0


@pytest.mark.parametrize("restriction", ["controlled_file", "foreign_owner", "unresolved_owner"])
@pytest.mark.parametrize("ledger_fallback", [False, True])
def test_review_hidden_archive_evidence_is_not_costed(cost_client, cost_db, restriction, ledger_fallback):
    # The current job is public; only its historical archive is restricted.
    if restriction == "controlled_file":
        restrict_historical_file(cost_db, 9006)
    elif restriction == "foreign_owner":
        cost_db.execute(text("UPDATE print_archives SET org_id=2 WHERE print_job_id=9006"))
    else:
        cost_db.execute(text("UPDATE print_archives SET ownership_state='unresolved' WHERE print_job_id=9006"))
    if not ledger_fallback:
        cost_db.execute(text("UPDATE print_archives SET consumption_json=:usage WHERE print_job_id=9006"),
                        {"usage": json.dumps([used(72, 30.0)])})
    cost_db.commit()
    body = cost_client.get("/jobs/806/cost").json()
    assert body["state"] == "partial"
    assert body["material"]["spools"] == [] and body["material"]["amount"] == 0
    assert body["difference"] is None
    if restriction == "controlled_file":
        cost_db.execute(text("INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,3)"))
        cost_db.commit()
        assert cost_client.get("/jobs/806/cost").json()["state"] == "complete"


def restrict_historical_file(db, attempt_id):
    db.execute(text("INSERT INTO print_files(id,filename,org_id) VALUES(809,'old.3mf',1)"))
    db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'secure')"))
    db.execute(text("INSERT INTO controlled_files(file_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                    "VALUES(809,'CUI',1,'Secure',7,'fixture',CURRENT_TIMESTAMP)"))
    db.execute(text("UPDATE print_archives SET print_file_id=809 WHERE print_job_id=:id"), {"id": attempt_id})


def test_review_deleted_spool_leaves_monitor_cost_partial(cost_client, cost_db):
    cost_db.execute(text("UPDATE print_jobs SET spool_snapshot=:snap WHERE id=9006"),
                    {"snap": snapshot(allocation(71, "20", "1000"), allocation(72, "30", "750"))})
    cost_db.execute(text("INSERT INTO spool_usage(spool_id,job_id,print_job_id,weight_used_g,notes) "
                        "VALUES(71,806,9006,100,'auto:moonraker: observed')"))
    cost_db.commit()
    before = cost_client.get("/jobs/806/cost").json()
    assert before["state"] == "complete" and before["material"]["amount"] == 3.20
    cost_db.execute(text("DELETE FROM spools WHERE id=71"))
    cost_db.commit()
    body = cost_client.get("/jobs/806/cost").json()
    assert body["state"] == "partial" and body["difference"] is None
    assert body["material"]["amount"] == 1.20 and body["total"] == 7.20
    assert {"part": "material", "reason": "usage_not_recorded", "attempt_id": 9006, "spool_id": 71} in body["unknown"]


@pytest.mark.parametrize("with_measured", [False, True])
def test_review_sliced_quantity_is_not_a_known_lower_bound(with_measured):
    usage = [dict(SPOOL_B, grams=30.0, provenance="sliced_estimate")]
    if with_measured:
        usage.append(dict(SPOOL_A, grams=50.0))
    result = cost([attempt(1, "completed", 2.0, *usage)])
    assert result["state"] == "partial"
    assert result["total"] == (6.75 if with_measured else 5.75)
    assert result["material"]["amount"] == (1.00 if with_measured else 0)
    assert result["material"]["spools"][0]["amount"] is None
    assert result["unknown"] == [{"part": "material", "reason": "usage_unmeasured", "attempt_id": 1, "spool_id": 72}]


def test_review_per_item_settings_scale_but_plate_handling_is_charged_once(cost_client, cost_db):
    config = dict(PRICING, packaging_cost=2)
    cost_db.execute(text("UPDATE system_config SET value=:value WHERE key='pricing_config'"), {"value": json.dumps(config)})
    cost_db.execute(text("UPDATE jobs SET quantity_on_bed=5 WHERE id=806"))
    cost_db.commit()
    body = cost_client.get("/jobs/806/cost").json()
    # Labour: (10 item minutes * 5 + 10 plate minutes) * 12/60 = 12.
    assert body["from_settings"] == {"amount": 25.75, "complete": True, "basis": "settings",
                                      "labor": 12, "packaging": 10, "other": 1.25, "overhead": 2.50}
    assert body["total"] == 27.70
