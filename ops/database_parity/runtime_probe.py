"""Exercise ODIN API, RBAC, WebSocket, and DBAPI paths on PostgreSQL."""

from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from starlette.testclient import TestClient

from core.db import engine
from core.db_utils import get_db
from main import app
from scripts.seed_release_gate import (
    ADMIN_EMAIL,
    OPERATOR_EMAIL,
    VIEWER_EMAIL,
    seed_release_gate_connection,
)


RUN_ID = "postgres-runtime-parity"


def _seed_monitor_graph(connection, label: str) -> dict[str, int]:
    """Create an isolated Education authority graph for contention probes."""
    suffix = f"{label}-{uuid.uuid4().hex[:10]}"
    org_id = int(
        connection.execute(
            text("INSERT INTO groups (name,is_org) VALUES (:name,TRUE) RETURNING id"),
            {"name": f"ODIN parity {suffix}"},
        ).scalar_one()
    )
    user_id = int(
        connection.execute(
            text(
                "INSERT INTO users (username,email,password_hash,role,is_active,group_id) "
                "VALUES (:username,:email,'fixture','viewer',TRUE,:org_id) RETURNING id"
            ),
            {
                "username": f"student-{suffix}",
                "email": f"student-{suffix}@example.invalid",
                "org_id": org_id,
            },
        ).scalar_one()
    )
    printer_id = int(
        connection.execute(
            text(
                "INSERT INTO printers "
                "(name,is_active,api_type,org_id,shared,slot_count,tags,timelapse_enabled) "
                "VALUES (:name,TRUE,'moonraker',:org_id,FALSE,4,CAST('[]' AS JSON),FALSE) "
                "RETURNING id"
            ),
            {"name": f"Parity printer {suffix}", "org_id": org_id},
        ).scalar_one()
    )
    center_id = int(
        connection.execute(
            text(
                "INSERT INTO education_cost_centers "
                "(org_id,name_key,code_key,display_name,code,state,revision,created_by) "
                "VALUES (:org_id,:key,:key,:name,:code,'active',1,:user_id) RETURNING id"
            ),
            {
                "org_id": org_id,
                "key": suffix,
                "name": f"Parity {suffix}",
                "code": suffix[:20],
                "user_id": user_id,
            },
        ).scalar_one()
    )
    connection.execute(
        text(
            "INSERT INTO education_cost_center_printers "
            "(org_id,cost_center_id,printer_id,state,granted_by) "
            "VALUES (:org_id,:center_id,:printer_id,'active',:user_id)"
        ),
        {
            "org_id": org_id,
            "center_id": center_id,
            "printer_id": printer_id,
            "user_id": user_id,
        },
    )
    operation_id = f"parity-{uuid.uuid4().hex}"
    connection.execute(
        text(
            "INSERT INTO education_upload_operations "
            "(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,expected_bytes,"
            "expected_hash,reservation_released_at) "
            "VALUES (:operation_id,:org_id,:user_id,'committed',1,1,1,:digest,CURRENT_TIMESTAMP)"
        ),
        {
            "operation_id": operation_id,
            "org_id": org_id,
            "user_id": user_id,
            "digest": uuid.uuid4().hex,
        },
    )
    model_id = int(
        connection.execute(
            text(
                "INSERT INTO models (name,org_id,default_filament_type) "
                "VALUES (:name,:org_id,'PLA') RETURNING id"
            ),
            {"name": f"Parity model {suffix}", "org_id": org_id},
        ).scalar_one()
    )
    file_id = int(
        connection.execute(
            text(
                "INSERT INTO print_files "
                "(filename,original_filename,org_id,created_by,storage_bytes,blob_state,model_id) "
                "VALUES ('opaque.gcode','fixture.gcode',:org_id,:user_id,1,'present',:model_id) "
                "RETURNING id"
            ),
            {"org_id": org_id, "user_id": user_id, "model_id": model_id},
        ).scalar_one()
    )
    job_id = int(
        connection.execute(
            text(
                "INSERT INTO jobs (model_id,item_name,status,printer_id,charged_to_user_id,"
                "charged_to_org_id,submitted_by,priority,quantity,is_locked,hold,required_tags,"
                "target_type,quantity_on_bed) "
                "VALUES (:model_id,:name,'scheduled',:printer_id,:user_id,:org_id,:user_id,3,"
                "1,FALSE,FALSE,CAST('[]' AS JSON),'specific',1) "
                "RETURNING id"
            ),
            {
                "model_id": model_id,
                "name": f"Parity job {suffix}",
                "printer_id": printer_id,
                "user_id": user_id,
                "org_id": org_id,
            },
        ).scalar_one()
    )
    submission_id = int(
        connection.execute(
            text(
                "INSERT INTO education_submissions "
                "(org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,"
                "approved_printer_id,approved_by,status,lifecycle_revision,compatibility_engine_version) "
                "VALUES (:org_id,:operation_id,:job_id,:file_id,:model_id,:center_id,:user_id,"
                ":printer_id,:user_id,'scheduled',3,'parity') RETURNING id"
            ),
            {
                "org_id": org_id,
                "operation_id": operation_id,
                "job_id": job_id,
                "file_id": file_id,
                "model_id": model_id,
                "center_id": center_id,
                "user_id": user_id,
                "printer_id": printer_id,
            },
        ).scalar_one()
    )
    return {
        "org_id": org_id,
        "submission_id": submission_id,
        "job_id": job_id,
        "printer_id": printer_id,
    }


def exercise_education_monitor_contention() -> dict[str, str]:
    """Prove checked-lock behavior with real competing PostgreSQL transactions."""
    from modules.organizations.education_policy import (
        claim_monitor_observation,
        confirm_dispatch_started,
        reserve_dispatch,
        reset_stale_schedule,
        terminal_monitor_observation,
    )

    assert engine.dialect.name == "postgresql"
    evidence: dict[str, str] = {}

    with engine.begin() as connection:
        reservation_graph = _seed_monitor_graph(connection, "reservation")
    reserved = threading.Event()
    reset_started = threading.Event()
    allow_commit = threading.Event()

    def reserve_worker():
        with engine.connect() as connection:
            transaction = connection.begin()
            result = reserve_dispatch(
                connection,
                job_id=reservation_graph["job_id"],
                printer_id=reservation_graph["printer_id"],
                expected_revision=3,
                extension=".gcode",
            )
            reserved.set()
            assert allow_commit.wait(10)
            transaction.commit()
            return result

    def reset_worker():
        assert reserved.wait(10)
        reset_started.set()
        with engine.connect() as connection:
            transaction = connection.begin()
            result = reset_stale_schedule(
                connection,
                submission_id=reservation_graph["submission_id"],
                job_id=reservation_graph["job_id"],
                printer_id=reservation_graph["printer_id"],
                expected_revision=3,
            )
            transaction.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        reservation_future = pool.submit(reserve_worker)
        assert reserved.wait(10)
        reset_future = pool.submit(reset_worker)
        assert reset_started.wait(10)
        allow_commit.set()
        reservation = reservation_future.result(timeout=15)
        reset_result = reset_future.result(timeout=15)
    assert reservation and reset_result is False
    evidence["reservation_vs_scheduler"] = "reserved_won_reset_refused"

    with engine.begin() as connection:
        claim_graph = _seed_monitor_graph(connection, "claim")
        claim_reservation = reserve_dispatch(
            connection,
            job_id=claim_graph["job_id"],
            printer_id=claim_graph["printer_id"],
            expected_revision=3,
            extension=".gcode",
        )
        assert claim_reservation
        assert confirm_dispatch_started(
            connection,
            claim_id=claim_reservation["claim_id"],
            submission_id=claim_graph["submission_id"],
            job_id=claim_graph["job_id"],
            printer_id=claim_graph["printer_id"],
            expected_revision=3,
        )["transitioned"]
        print_job_id = int(
            connection.execute(
                text(
                    "INSERT INTO print_jobs (printer_id,filename,job_name,started_at,status) "
                    "VALUES (:printer_id,:filename,:filename,CURRENT_TIMESTAMP,'running') RETURNING id"
                ),
                {
                    "printer_id": claim_graph["printer_id"],
                    "filename": claim_reservation["remote_filename"],
                },
            ).scalar_one()
        )
    authority_changed = threading.Event()
    allow_authority_commit = threading.Event()

    def authority_worker():
        with engine.connect() as connection:
            transaction = connection.begin()
            changed = connection.execute(
                text(
                    "UPDATE education_submissions SET lifecycle_revision=lifecycle_revision+1 "
                    "WHERE id=:submission_id AND status='printing' AND lifecycle_revision=4"
                ),
                {"submission_id": claim_graph["submission_id"]},
            )
            assert changed.rowcount == 1
            authority_changed.set()
            assert allow_authority_commit.wait(10)
            transaction.commit()

    def claim_worker():
        assert authority_changed.wait(10)
        with engine.connect() as connection:
            transaction = connection.begin()
            result = claim_monitor_observation(
                connection,
                print_job_id=print_job_id,
                printer_id=claim_graph["printer_id"],
                observed_filename=claim_reservation["remote_filename"],
            )
            transaction.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        authority_future = pool.submit(authority_worker)
        assert authority_changed.wait(10)
        claim_future = pool.submit(claim_worker)
        allow_authority_commit.set()
        authority_future.result(timeout=15)
        claim_result = claim_future.result(timeout=15)
    assert claim_result["authorized"] is False
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT scheduled_job_id FROM print_jobs WHERE id=:id"), {"id": print_job_id}
        ).scalar_one_or_none() is None
    evidence["claim_vs_authority"] = "authority_drift_won_claim_rolled_back"

    with engine.begin() as connection:
        terminal_graph = _seed_monitor_graph(connection, "terminal")
        terminal_reservation = reserve_dispatch(
            connection,
            job_id=terminal_graph["job_id"],
            printer_id=terminal_graph["printer_id"],
            expected_revision=3,
            extension=".gcode",
        )
        assert terminal_reservation
        assert confirm_dispatch_started(
            connection,
            claim_id=terminal_reservation["claim_id"],
            submission_id=terminal_graph["submission_id"],
            job_id=terminal_graph["job_id"],
            printer_id=terminal_graph["printer_id"],
            expected_revision=3,
        )["transitioned"]
        terminal_print_job_id = int(
            connection.execute(
                text(
                    "INSERT INTO print_jobs (printer_id,filename,job_name,started_at,status) "
                    "VALUES (:printer_id,:filename,:filename,CURRENT_TIMESTAMP,'running') RETURNING id"
                ),
                {
                    "printer_id": terminal_graph["printer_id"],
                    "filename": terminal_reservation["remote_filename"],
                },
            ).scalar_one()
        )
        assert claim_monitor_observation(
            connection,
            print_job_id=terminal_print_job_id,
            printer_id=terminal_graph["printer_id"],
            observed_filename=terminal_reservation["remote_filename"],
        )["authorized"]
    terminal_barrier = threading.Barrier(2)

    def terminal_worker():
        terminal_barrier.wait(timeout=10)
        with engine.connect() as connection:
            transaction = connection.begin()
            result = terminal_monitor_observation(
                connection,
                print_job_id=terminal_print_job_id,
                printer_id=terminal_graph["printer_id"],
                terminal_status="completed",
            )
            transaction.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        terminal_results = [
            future.result(timeout=15)
            for future in (pool.submit(terminal_worker), pool.submit(terminal_worker))
        ]
    assert sum(bool(item.get("transitioned")) for item in terminal_results) == 1
    assert sum(bool(item.get("idempotent")) for item in terminal_results) == 1
    evidence["duplicate_terminal"] = "one_transition_one_idempotent"
    return evidence


def login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", data={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    if os.environ.get("API_KEY"):
        headers["X-API-Key"] = os.environ["API_KEY"]
    return headers


def items(response) -> list[dict]:
    assert response.status_code == 200, response.text
    payload = response.json()
    if isinstance(payload, list):
        return payload
    for key in ("items", "jobs", "data", "results", "profiles"):
        if isinstance(payload.get(key), list):
            return payload[key]
    raise AssertionError(f"response does not contain an item list: {payload}")


def validate_domain_graph(client: TestClient, headers: dict[str, str]) -> dict[str, int]:
    printers = items(client.get("/api/printers", headers=headers))
    printer = next(item for item in printers if item["name"] == "ODIN Candidate Gate Printer")
    spools = items(client.get("/api/spools", headers=headers))
    assert any(item.get("qr_code") == "ODIN-CANDIDATE-SPOOL" for item in spools)
    models = items(client.get("/api/models", headers=headers))
    model = next(item for item in models if item["name"] == "ODIN Candidate Calibration Cube")
    products = items(client.get("/api/products", headers=headers))
    product = next(item for item in products if item.get("sku") == "ODIN-CANDIDATE-001")
    product_detail = client.get(f"/api/products/{product['id']}", headers=headers)
    assert product_detail.status_code == 200, product_detail.text
    assert product_detail.json()["components"][0]["model_name"] == model["name"]
    orders = items(client.get("/api/orders", headers=headers))
    order = next(
        item
        for item in orders
        if item.get("order_number") == "ODIN-CANDIDATE-ORDER-001"
    )
    order_detail = client.get(f"/api/orders/{order['id']}", headers=headers)
    assert order_detail.status_code == 200, order_detail.text
    assert order_detail.json()["items"][0]["product_id"] == product["id"]
    jobs = items(client.get("/api/jobs", headers=headers))
    candidate_jobs = [
        item for item in jobs if item["item_name"].startswith("ODIN Candidate Cube")
    ]
    assert {item["status"] for item in candidate_jobs} == {"pending", "completed"}

    with engine.connect() as connection:
        inventory_links = connection.execute(
            text(
                "SELECT COUNT(*) FROM printers p "
                "JOIN filament_slots fs ON fs.printer_id=p.id "
                "JOIN spools s ON s.id=fs.assigned_spool_id "
                "JOIN filament_library f ON f.id=s.filament_id "
                "WHERE p.name='ODIN Candidate Gate Printer' "
                "AND s.qr_code='ODIN-CANDIDATE-SPOOL'"
            )
        ).scalar_one()
        order_links = connection.execute(
            text(
                "SELECT COUNT(*) FROM jobs j "
                "JOIN models m ON m.id=j.model_id "
                "JOIN order_items oi ON oi.id=j.order_item_id "
                "JOIN orders o ON o.id=oi.order_id "
                "JOIN product_components pc ON pc.product_id=oi.product_id "
                "WHERE m.name='ODIN Candidate Calibration Cube' "
                "AND o.order_number='ODIN-CANDIDATE-ORDER-001'"
            )
        ).scalar_one()
    assert inventory_links == 1
    assert order_links == 2
    return {"printer": int(printer["id"]), "model": int(model["id"])}


def exercise_cross_domain_workflow(
    client: TestClient,
    admin: dict[str, str],
    operator: dict[str, str],
    viewer: dict[str, str],
    ids: dict[str, int],
) -> None:
    users = items(client.get("/api/users", headers=admin))
    viewer_id = next(item["id"] for item in users if item["username"] == VIEWER_EMAIL)

    first_org = client.post(
        "/api/orgs",
        json={"name": "ODIN Parity Classroom", "description": "synthetic"},
        headers=admin,
    )
    second_org = client.post(
        "/api/orgs",
        json={"name": "ODIN Parity Hidden Classroom", "description": "synthetic"},
        headers=admin,
    )
    assert first_org.status_code == 200, first_org.text
    assert second_org.status_code == 200, second_org.text
    first_org_id = int(first_org.json()["id"])
    second_org_id = int(second_org.json()["id"])
    assert client.post(
        f"/api/orgs/{first_org_id}/members",
        json={"user_id": viewer_id},
        headers=admin,
    ).status_code == 200
    assert client.post(
        f"/api/orgs/{first_org_id}/printers",
        json={"printer_id": ids["printer"]},
        headers=admin,
    ).status_code == 200

    visible_project = client.post(
        "/api/projects",
        json={"name": "ODIN Visible Project", "org_id": first_org_id},
        headers=admin,
    )
    hidden_project = client.post(
        "/api/projects",
        json={"name": "ODIN Hidden Project", "org_id": second_org_id},
        headers=admin,
    )
    assert visible_project.status_code == 200, visible_project.text
    assert hidden_project.status_code == 200, hidden_project.text
    viewer_projects = {item["name"] for item in items(client.get("/api/projects", headers=viewer))}
    assert "ODIN Visible Project" in viewer_projects
    assert "ODIN Hidden Project" not in viewer_projects

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO models (name, default_filament_type, org_id) "
                "VALUES ('ODIN Hidden Tenant Model', 'PLA', :org_id)"
            ),
            {"org_id": second_org_id},
        )
    viewer_models = {item["name"] for item in items(client.get("/api/models", headers=viewer))}
    assert "ODIN Hidden Tenant Model" not in viewer_models
    admin_models = {item["name"] for item in items(client.get("/api/models", headers=admin))}
    assert "ODIN Hidden Tenant Model" in admin_models

    webhook = client.post(
        "/api/webhooks",
        json={
            "name": "ODIN Parity Webhook",
            "url": "https://example.com/odin-parity",
            "webhook_type": "discord",
        },
        headers=admin,
    )
    assert webhook.status_code == 200, webhook.text
    webhooks = items(client.get("/api/webhooks", headers=admin))
    webhook_id = next(item["id"] for item in webhooks if item["name"] == "ODIN Parity Webhook")
    assert client.delete(f"/api/webhooks/{webhook_id}", headers=admin).status_code == 200

    profile = client.post(
        "/api/profiles",
        json={
            "name": "ODIN Parity Profile",
            "slicer": "orca",
            "category": "process",
            "raw_content": "{}",
        },
        headers=operator,
    )
    assert profile.status_code == 201, profile.text
    profile_id = int(profile.json()["id"])
    assert client.delete(f"/api/profiles/{profile_id}", headers=operator).status_code == 204

    report = client.post(
        "/api/report-schedules",
        json={
            "name": "ODIN Parity Report",
            "report_type": "job_summary",
            "frequency": "weekly",
            "recipients": ["parity@example.invalid"],
        },
        headers=admin,
    )
    assert report.status_code == 200, report.text
    report_id = int(report.json()["id"])
    assert client.delete(f"/api/report-schedules/{report_id}", headers=admin).status_code == 200

    uploaded = client.post(
        "/api/print-files/upload",
        files={"file": ("odin-parity.gcode", b"; synthetic parity gcode\nG28\n", "text/plain")},
        headers=operator,
    )
    assert uploaded.status_code == 200, uploaded.text
    upload_id = int(uploaded.json()["id"])
    assert client.delete(f"/api/print-files/{upload_id}", headers=operator).status_code == 200

    assert client.post("/api/backups", headers=viewer).status_code == 403
    backup = client.post("/api/backups", headers=admin)
    assert backup.status_code == 200, backup.text
    backup_name = backup.json()["filename"]
    assert any(
        item["filename"] == backup_name
        for item in items(client.get("/api/backups", headers=admin))
    )
    assert client.delete(f"/api/backups/{backup_name}", headers=admin).status_code == 204


def verify_restored_runtime(
    client: TestClient, passwords: dict[str, str], expected_dialect: str
) -> None:
    admin = login(client, ADMIN_EMAIL, passwords[ADMIN_EMAIL])
    ids = validate_domain_graph(client, admin)
    created = client.post(
        "/api/models",
        json={"name": f"ODIN {expected_dialect} Post-Restore Write"},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    model_id = int(created.json()["id"])
    assert client.delete(f"/api/models/{model_id}", headers=admin).status_code == 204
    assert ids["printer"] > 0


def run() -> None:
    expected_dialect = os.getenv("ODIN_EXPECTED_DATABASE_DIALECT", "postgresql")
    passwords = {
        ADMIN_EMAIL: os.environ["ODIN_CANDIDATE_ADMIN_PASSWORD"],
        OPERATOR_EMAIL: os.environ["ODIN_CANDIDATE_OPERATOR_PASSWORD"],
        VIEWER_EMAIL: os.environ["ODIN_CANDIDATE_VIEWER_PASSWORD"],
    }
    with TestClient(app, base_url="http://school.test") as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["ready"] is True
        assert ready.json()["dialect"] == expected_dialect

        if os.getenv("ODIN_PARITY_MODE") == "verify-restored":
            verify_restored_runtime(client, passwords, expected_dialect)
            print(
                f"{expected_dialect}-restored-runtime: PASS authentication "
                "relationship-graph continued-read-write"
            )
            return

        setup = client.post(
            "/api/setup/admin",
            json={
                "username": ADMIN_EMAIL,
                "email": ADMIN_EMAIL,
                "password": passwords[ADMIN_EMAIL],
                "role": "admin",
            },
        )
        assert setup.status_code == 200, setup.text

        with get_db() as connection:
            try:
                counts = seed_release_gate_connection(
                    connection=connection,
                    run_id=RUN_ID,
                    marker=RUN_ID,
                    admin_email=ADMIN_EMAIL,
                    admin_password=passwords[ADMIN_EMAIL],
                    operator_email=OPERATOR_EMAIL,
                    operator_password=passwords[OPERATOR_EMAIL],
                    viewer_email=VIEWER_EMAIL,
                    viewer_password=passwords[VIEWER_EMAIL],
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        assert counts["users"] == 3

        client.cookies.clear()
        anonymous = client.get("/api/printers")
        assert anonymous.status_code == 401, (
            anonymous.status_code,
            anonymous.text[:300],
        )

        admin = login(client, ADMIN_EMAIL, passwords[ADMIN_EMAIL])
        operator = login(client, OPERATOR_EMAIL, passwords[OPERATOR_EMAIL])
        viewer = login(client, VIEWER_EMAIL, passwords[VIEWER_EMAIL])
        client.cookies.clear()

        admin_users = client.get("/api/users", headers=admin)
        assert admin_users.status_code == 200, (
            admin_users.status_code,
            admin_users.text[:300],
        )
        assert client.get("/api/users", headers=operator).status_code == 403
        assert client.get("/api/users", headers=viewer).status_code == 403
        assert (
            client.post(
                "/api/models", json={"name": "Viewer mutation"}, headers=viewer
            ).status_code
            == 403
        )

        ids = validate_domain_graph(client, viewer)
        exercise_cross_domain_workflow(client, admin, operator, viewer, ids)
        education_contention: dict[str, str] = {}
        if expected_dialect == "postgresql":
            education_contention = exercise_education_monitor_contention()
            assert set(education_contention) == {
                "reservation_vs_scheduler",
                "claim_vs_authority",
                "duplicate_terminal",
            }

        created = client.post(
            "/api/models",
            json={
                "name": "PostgreSQL Runtime Model",
                "build_time_hours": 0.25,
                "default_filament_type": "PLA",
            },
            headers=operator,
        )
        assert created.status_code == 201, created.text
        model_id = created.json()["id"]
        updated = client.patch(
            f"/api/models/{model_id}",
            json={"notes": "postgres parity round trip"},
            headers=operator,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["notes"] == "postgres parity round trip"
        assert (
            client.delete(f"/api/models/{model_id}", headers=operator).status_code
            == 204
        )

        token_response = client.post("/api/auth/ws-token", headers=admin)
        assert token_response.status_code == 200, token_response.text
        with client.websocket_connect(
            f"/api/v1/ws?token={token_response.json()['token']}",
            headers={"origin": "http://school.test", "host": "school.test"},
        ) as websocket:
            websocket.send_text("ping")
            assert websocket.receive_text() == "pong"
            websocket.send_text('{"type":"subscribe","channels":["events"]}')
            assert json.loads(websocket.receive_text())["type"] == "subscribed"

        with get_db() as connection:
            connection.execute(
                "INSERT INTO system_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("database_parity_background_write", json.dumps("passed")),
            )
            connection.commit()
        with engine.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT value FROM system_config "
                    "WHERE key='database_parity_background_write'"
                )
            ).scalar_one()
        assert value == "passed" or value == '"passed"'

    print(
        f"{expected_dialect}-runtime: PASS readiness setup personas rbac crud "
        "organization-scope relationship-graph identity-paths backup-authorization "
        "websocket background-write education-monitor-contention="
        f"{json.dumps(education_contention, sort_keys=True)}"
    )


if __name__ == "__main__":
    run()
