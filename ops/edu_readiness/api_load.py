"""Deterministic in-process EDU API load gate against the real ASGI app."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

try:
    from .common import utc_now, write_result
except ImportError:
    from common import utc_now, write_result

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
THRESHOLDS_PATH = Path(__file__).with_name("thresholds.json")


@dataclass(frozen=True)
class Operation:
    category: str
    method: str
    path: str
    persona: str
    expected: int
    body: dict[str, Any] | None = None
    forbidden_ids: tuple[int, ...] = ()


def workload(approval_ids: list[int], repetition: int) -> list[Operation]:
    operations: list[Operation] = []
    operations += [Operation("auth", "GET", "/api/auth/me", "viewer", 200) for _ in range(100)]
    operations += [Operation("auth", "GET", "/api/permissions", "viewer", 200) for _ in range(100)]
    operations += [Operation("printers", "GET", "/api/printers", "viewer", 200, forbidden_ids=(9002,)) for _ in range(250)]
    operations += [Operation("jobs", "GET", "/api/jobs?limit=50", "viewer", 200, forbidden_ids=(9002,)) for _ in range(200)]
    operations += [Operation("reports", "GET", "/api/quotas", "viewer", 200) for _ in range(75)]
    operations += [Operation("reports", "GET", "/api/education/usage-report?days=30", "admin", 200) for _ in range(75)]
    operations += [
        Operation(
            "job_create",
            "POST",
            "/api/jobs",
            "viewer",
            201,
            {"item_name": f"synthetic-load-{repetition}-{index}", "quantity": 1, "duration_hours": 0.25},
        )
        for index in range(75)
    ]
    operations += [
        Operation("job_approve", "POST", f"/api/jobs/{job_id}/approve", "operator", 200)
        for job_id in approval_ids[:75]
    ]
    operations += [Operation("session_churn", "POST", "/api/auth/logout", "churn", 200) for _ in range(50)]
    assert len(operations) == 1000
    random.Random(20260911 + repetition).shuffle(operations)
    return operations


def warmup_workload() -> list[Operation]:
    """Warm every measured request class before collecting latency evidence."""
    operations: list[Operation] = []
    operations += [Operation("auth", "GET", "/api/auth/me", "viewer", 200) for _ in range(10)]
    operations += [Operation("auth", "GET", "/api/permissions", "viewer", 200) for _ in range(10)]
    operations += [Operation("printers", "GET", "/api/printers", "viewer", 200) for _ in range(25)]
    operations += [Operation("jobs", "GET", "/api/jobs?limit=50", "viewer", 200) for _ in range(20)]
    operations += [Operation("reports", "GET", "/api/quotas", "viewer", 200) for _ in range(8)]
    operations += [Operation("reports", "GET", "/api/education/usage-report?days=30", "admin", 200) for _ in range(7)]
    operations += [
        Operation(
            "job_create", "POST", "/api/jobs", "viewer", 201,
            {"item_name": f"synthetic-warmup-{index}", "quantity": 1, "duration_hours": 0.25},
        )
        for index in range(8)
    ]
    operations += [
        Operation("job_approve", "POST", f"/api/jobs/{job_id}/approve", "operator", 200)
        for job_id in range(10290, 10297)
    ]
    operations += [Operation("session_churn", "POST", "/api/auth/logout", "warmup_churn", 200) for _ in range(5)]
    assert len(operations) == 100
    random.Random(20260910).shuffle(operations)
    return operations


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def _fd_count() -> int:
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return 0


def _prepare_database(path: Path):
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ["DATABASE_PATH"] = str(path)
    os.environ["JWT_SECRET_KEY"] = "edu-load-jwt-secret-that-is-test-only"
    os.environ["COOKIE_SECURE"] = "false"
    os.environ["LICENSE_DIR"] = str(path.parent / "license")
    os.environ["CORS_ORIGINS"] = ""

    from core.base import Base
    from core.db import engine, run_core_migrations, run_module_migrations
    import core.models  # noqa: F401
    import modules.archives.models  # noqa: F401
    import modules.inventory.models  # noqa: F401
    import modules.jobs.models  # noqa: F401
    import modules.models_library.models  # noqa: F401
    import modules.notifications.models  # noqa: F401
    import modules.orders.models  # noqa: F401
    import modules.printers.models  # noqa: F401
    import modules.system.models  # noqa: F401
    import modules.vision.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    run_core_migrations(os.environ["DATABASE_URL"])
    run_module_migrations(ROOT / "backend" / "modules", os.environ["DATABASE_URL"])

    from core.auth import create_access_token, hash_password
    from sqlalchemy import text

    password_hash = hash_password("Synthetic-Edu-Load-Only-2026!")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO groups (id, name, is_org) VALUES (1, 'Synthetic School A', 1), (2, 'Synthetic School B', 1)"))
        users = []
        for index in range(1, 31):
            role = "viewer" if index <= 25 else ("operator" if index <= 29 else "admin")
            group_id = 1 if index != 29 else 2
            users.append({
                "id": index,
                "username": f"load-user-{index}@school.test",
                "email": f"load-user-{index}@school.test",
                "password_hash": password_hash,
                "role": role,
                "group_id": group_id,
            })
        connection.execute(text(
            "INSERT INTO users (id, username, email, password_hash, role, is_active, group_id, quota_jobs, quota_period) "
            "VALUES (:id, :username, :email, :password_hash, :role, 1, :group_id, 10000, 'monthly')"
        ), users)
        connection.execute(text(
            "UPDATE groups SET owner_id = CASE id WHEN 1 THEN 26 WHEN 2 THEN 29 END "
            "WHERE id IN (1, 2)"
        ))
        connection.execute(text(
            "INSERT INTO printers "
            "(id, name, model, api_type, is_active, org_id, shared, slot_count, tags, timelapse_enabled) VALUES "
            "(9001, 'Synthetic-A', 'Fixture', 'bambu', 1, 1, 0, 4, '[]', 0), "
            "(9002, 'Synthetic-B', 'Fixture', 'moonraker', 1, 2, 0, 4, '[]', 0), "
            "(9003, 'Synthetic-Shared', 'Fixture', 'prusalink', 1, 2, 1, 4, '[]', 0)"
        ))
        seed_jobs = [
            {"id": 10000 + i, "name": f"seed-{i}", "org": 1, "user": (i % 25) + 1}
            for i in range(300)
        ]
        seed_jobs.append({"id": 9002, "name": "foreign-tenant-canary", "org": 2, "user": 29})
        connection.execute(text(
            "INSERT INTO jobs "
            "(id, item_name, quantity, priority, status, duration_hours, submitted_by, charged_to_user_id, charged_to_org_id, hold, is_locked) "
            "VALUES (:id, :name, 1, 3, 'submitted', 0.25, :user, :user, :org, 0, 0)"
        ), seed_jobs)
        connection.execute(text("INSERT INTO system_config (key, value) VALUES ('require_job_approval', 'true')"))

    import license_manager
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    signing_key = Ed25519PrivateKey.generate()
    license_manager.ODIN_PUBLIC_KEY = signing_key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    payload_bytes = json.dumps(
        {
            "tier": "education",
            "licensee": "Synthetic EDU Load Fixture",
            "email": "load-license@school.test",
            "expires_at": "2099-12-31",
            "max_printers": 9999,
            "max_users": 9999,
            "features": list(license_manager.TIERS["education"]["features"]),
        },
        sort_keys=True,
    ).encode("utf-8")
    signature = signing_key.sign(payload_bytes)
    encoded_license = (
        base64.urlsafe_b64encode(payload_bytes).decode("ascii")
        + "."
        + base64.urlsafe_b64encode(signature).decode("ascii")
    )
    license_path = Path(os.environ["LICENSE_DIR"])
    license_path.mkdir(parents=True, exist_ok=True)
    (license_path / "odin.license").write_text(encoded_license, encoding="utf-8")
    license_manager._cached_license = None
    license_manager._cached_mtime = 0
    verified_license = license_manager.get_license()
    if not verified_license.valid or verified_license.tier != "education":
        raise RuntimeError(f"ephemeral signed EDU license failed validation: {verified_license.error}")

    tokens = {
        "viewer": [create_access_token({"sub": f"load-user-{i}@school.test", "role": "viewer"}) for i in range(1, 26)],
        "operator": [create_access_token({"sub": f"load-user-{i}@school.test", "role": "operator"}) for i in range(26, 29)],
        "admin": [create_access_token({"sub": "load-user-30@school.test", "role": "admin"})],
        "churn": [create_access_token({"sub": "load-user-25@school.test", "role": "viewer"}) for _ in range(150)],
        "warmup_churn": [create_access_token({"sub": "load-user-24@school.test", "role": "viewer"}) for _ in range(5)],
        "ws_target": [create_access_token({"sub": "load-user-1@school.test", "role": "viewer", "ws": True})],
        "ws_foreign": [create_access_token({"sub": "load-user-29@school.test", "role": "operator", "ws": True})],
    }
    from core.app import create_app
    return create_app(), tokens


async def _execute_repetition(app, tokens, operations, concurrency):
    semaphore = asyncio.Semaphore(concurrency)
    timings: dict[str, list[float]] = defaultdict(list)
    errors = []
    tenant_leaks = 0
    token_offsets = Counter()

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://odin.test") as client:
        async def execute(index: int, operation: Operation):
            nonlocal tenant_leaks
            async with semaphore:
                pool = tokens[operation.persona]
                offset = token_offsets[operation.persona]
                token_offsets[operation.persona] += 1
                token = pool[offset % len(pool)]
                started = time.perf_counter()
                response = await client.request(
                    operation.method,
                    operation.path,
                    headers={"Authorization": f"Bearer {token}"},
                    json=operation.body,
                )
                elapsed = (time.perf_counter() - started) * 1000
                timings[operation.category].append(elapsed)
                if response.status_code != operation.expected:
                    errors.append({"category": operation.category, "status": response.status_code})
                    return
                if operation.forbidden_ids:
                    body = response.json()
                    encoded = json.dumps(body, sort_keys=True)
                    if any(f'"id": {item}' in encoded for item in operation.forbidden_ids):
                        tenant_leaks += 1

        started = time.perf_counter()
        await asyncio.gather(*(execute(index, operation) for index, operation in enumerate(operations)))
        duration = time.perf_counter() - started
    return timings, errors, tenant_leaks, duration


def _websocket_cycles(app, target_token: str, foreign_token: str, cycles: int) -> dict[str, Any]:
    from anyio import WouldBlock
    from starlette.testclient import TestClient
    from core.app import ws_manager

    timings = []
    failures = 0
    subscription_acks = 0
    messages_exchanged = 0
    isolation_checks = 0
    cross_user_events = 0
    with TestClient(app) as client:
        for cycle in range(cycles):
            started = time.perf_counter()
            try:
                with client.websocket_connect(f"/api/v1/ws?token={target_token}") as target_ws:
                    with client.websocket_connect(f"/api/v1/ws?token={foreign_token}") as foreign_ws:
                        timings.append((time.perf_counter() - started) * 1000)
                        for session in (target_ws, foreign_ws):
                            session.send_json({"type": "subscribe", "channels": ["events"]})
                            if session.receive_json() != {"type": "subscribed", "channels": ["events"]}:
                                raise AssertionError("WebSocket subscription acknowledgement mismatch")
                            subscription_acks += 1
                            messages_exchanged += 2
                            session.send_text("ping")
                            if session.receive_text() != "pong":
                                raise AssertionError("WebSocket ping exchange failed")
                            messages_exchanged += 2

                        probe = {
                            "type": "privacy_probe",
                            "data": {"cycle": cycle},
                            "_audience": {"user_ids": [1]},
                        }
                        delivered = client.portal.call(ws_manager.broadcast, probe)
                        received = target_ws.receive_json()
                        messages_exchanged += 1
                        if delivered != 1 or received != {"type": "privacy_probe", "data": {"cycle": cycle}}:
                            raise AssertionError("targeted WebSocket delivery mismatch")
                        isolation_checks += 1
                        try:
                            leaked = foreign_ws._send_rx.receive_nowait()
                        except WouldBlock:
                            leaked = None
                        if leaked and leaked.get("type") == "websocket.send":
                            cross_user_events += 1
            except Exception:
                failures += 1
    return {
        "timings": timings,
        "failures": failures,
        "subscription_acks": subscription_acks,
        "messages_exchanged": messages_exchanged,
        "isolation_checks": isolation_checks,
        "cross_user_events": cross_user_events,
    }


async def run(run_id: str, output: Path) -> int:
    thresholds_bytes = THRESHOLDS_PATH.read_bytes()
    thresholds = json.loads(thresholds_bytes)
    started_at = utc_now()
    started = time.perf_counter()
    findings = []
    all_metrics = []
    tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="odin-edu-load-") as directory:
        database = Path(directory) / "odin.db"
        app, tokens = _prepare_database(database)
        warmup = warmup_workload()
        if len(warmup) != thresholds["warmup_operations"]:
            raise RuntimeError("committed warmup workload does not match threshold policy")
        _, warmup_errors, warmup_leaks, _ = await _execute_repetition(
            app, tokens, warmup, thresholds["concurrent_users"]
        )
        if warmup_errors:
            findings.append(f"warmup returned {len(warmup_errors)} unexpected responses")
        if warmup_leaks:
            findings.append(f"warmup exposed {warmup_leaks} cross-tenant records")
        memory_baseline = tracemalloc.get_traced_memory()[0]
        fd_baseline = _fd_count()
        for repetition in range(thresholds["repetitions"]):
            ids = list(range(10000 + repetition * 75, 10000 + (repetition + 1) * 75))
            operations = workload(ids, repetition)
            repetition_tokens = dict(tokens)
            repetition_tokens["churn"] = tokens["churn"][repetition * 50:(repetition + 1) * 50]
            timings, errors, leaks, duration = await _execute_repetition(
                app, repetition_tokens, operations, thresholds["concurrent_users"]
            )
            ws_metrics = _websocket_cycles(
                app,
                tokens["ws_target"][0],
                tokens["ws_foreign"][0],
                thresholds["websocket_cycles_per_repetition"],
            )
            ws_times = ws_metrics["timings"]
            read_times = [value for key, values in timings.items() if key in {"auth", "printers", "jobs", "reports"} for value in values]
            write_times = [value for key, values in timings.items() if key not in {"auth", "printers", "jobs", "reports"} for value in values]
            global_times = read_times + write_times
            metric = {
                "repetition": repetition + 1,
                "operations": len(global_times),
                "reads": len(read_times),
                "writes": len(write_times),
                "duration_seconds": round(duration, 3),
                "read_p95_ms": _percentile(read_times, 0.95),
                "write_p95_ms": _percentile(write_times, 0.95),
                "global_p99_ms": _percentile(global_times, 0.99),
                "errors": len(errors),
                "unexpected_5xx": sum(1 for error in errors if error["status"] >= 500),
                "tenant_leaks": leaks,
                "websocket_cycles": len(ws_times),
                "websocket_failures": ws_metrics["failures"],
                "websocket_p95_ms": _percentile(ws_times, 0.95),
                "websocket_subscription_acks": ws_metrics["subscription_acks"],
                "websocket_messages_exchanged": ws_metrics["messages_exchanged"],
                "websocket_isolation_checks": ws_metrics["isolation_checks"],
                "websocket_cross_user_events": ws_metrics["cross_user_events"],
                "classes": {key: len(value) for key, value in timings.items()},
                "class_latency_ms": {
                    key: {
                        "count": len(values),
                        "p50": _percentile(values, 0.50),
                        "p95": _percentile(values, 0.95),
                        "p99": _percentile(values, 0.99),
                        "maximum": round(max(values), 3) if values else 0.0,
                    }
                    for key, values in timings.items()
                },
            }
            all_metrics.append(metric)
            checks = {
                "operation count": metric["operations"] == thresholds["operations_per_repetition"],
                "read minimum": metric["reads"] >= thresholds["minimum_reads"],
                "write minimum": metric["writes"] >= thresholds["minimum_writes"],
                "duration": metric["duration_seconds"] <= thresholds["maximum_repetition_seconds"],
                "read p95": metric["read_p95_ms"] <= thresholds["read_p95_ms"],
                "write p95": metric["write_p95_ms"] <= thresholds["write_p95_ms"],
                "global p99": metric["global_p99_ms"] <= thresholds["global_p99_ms"],
                "errors": metric["errors"] == thresholds["maximum_error_rate"],
                "5xx": metric["unexpected_5xx"] == thresholds["maximum_5xx"],
                "tenant leaks": metric["tenant_leaks"] == thresholds["maximum_tenant_leaks"],
                "websocket count": metric["websocket_cycles"] == thresholds["websocket_cycles_per_repetition"],
                "websocket failures": metric["websocket_failures"] == 0,
                "websocket p95": metric["websocket_p95_ms"] <= thresholds["websocket_p95_ms"],
                "websocket subscriptions": metric["websocket_subscription_acks"] == thresholds["websocket_cycles_per_repetition"] * 2,
                "websocket messages": metric["websocket_messages_exchanged"] >= thresholds["websocket_cycles_per_repetition"] * 9,
                "websocket isolation checks": metric["websocket_isolation_checks"] == thresholds["websocket_cycles_per_repetition"],
                "websocket cross-user isolation": metric["websocket_cross_user_events"] == 0,
                "class coverage": all(metric["classes"].get(name, 0) > 0 for name in ("auth", "printers", "jobs", "reports", "job_create", "job_approve", "session_churn")),
            }
            findings.extend(
                f"repetition {repetition + 1}: {name}"
                for name, passed in checks.items() if not passed
            )
        connection = sqlite3.connect(database)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        memory_delta = tracemalloc.get_traced_memory()[0] - memory_baseline
        fd_delta = _fd_count() - fd_baseline
        if integrity != "ok":
            findings.append("post-run SQLite integrity check failed")
        if memory_delta > thresholds["maximum_memory_delta_mib"] * 1024 * 1024:
            findings.append("tracked memory growth exceeded threshold")
        if fd_delta > thresholds["maximum_fd_delta"]:
            findings.append("file descriptor growth exceeded threshold")
    if THRESHOLDS_PATH.read_bytes() != thresholds_bytes:
        findings.append("threshold file changed during run")
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "gate_id": "api_load",
        "mandatory": True,
        "status": "pass" if not findings else "fail",
        "started_at": started_at,
        "ended_at": utc_now(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "tool_versions": {"python": os.sys.version.split()[0], "httpx": httpx.__version__},
        "executed_count": sum(metric["operations"] + metric["websocket_cycles"] for metric in all_metrics),
        "skipped_count": 0,
        "xfailed_count": 0,
        "metrics": {"repetitions": all_metrics, "integrity_check": integrity, "memory_delta_bytes": memory_delta, "fd_delta": fd_delta},
        "findings": findings,
        "artifacts": ["api_load.json"],
    }
    write_result(output.parent, result)
    return 0 if result["status"] == "pass" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(run(args.run_id, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
