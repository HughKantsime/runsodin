"""Profiler accounting tests use only in-memory databases and in-process ASGI."""
import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from ops.edu_readiness.request_profile import CLASSES, category, compare, install_framework, render_text, summarize
from ops.edu_readiness.request_profile_layers import Profile, Sample, span


def test_nested_layers_are_exclusive_and_sum_to_total_within_five_percent():
    sample = Sample("auth", 0, 10, [
        (1, 9, "middleware", 1), (2, 8, "auth", 2), (3, 5, "database", 3),
    ])
    layers = sample.partition()
    assert layers == {"framework_wait": 2000, "middleware": 2000, "auth": 4000, "database": 2000}
    assert sum(layers.values()) == pytest.approx((sample.ended - sample.started) * 1000, rel=.05)


def test_overlapping_tasks_and_clipped_intervals_do_not_double_count():
    sample = Sample("jobs", 0, 10, [
        (-1, 20, "middleware", 1), (2, 8, "first", 2), (4, 9, "second", 2),
    ])
    assert sample.partition() == {"middleware": 3000, "first": 2000, "second": 5000}


def test_exception_restores_context_and_patches():
    def fail():
        raise ValueError("expected")
    target = SimpleNamespace(call=fail)
    profile = Profile()
    profile.wrap(target, "call", "route_handler")
    with pytest.raises(ValueError), profile.request("jobs"):
        target.call()
    profile.restore()
    assert target.call is fail
    with span("outside_request"):
        pass
    assert len(profile.samples) == 1
    assert profile.samples[0].partition()["route_handler"] > 0


def test_sql_events_count_errors_and_remove_listeners():
    engine = create_engine("sqlite:///:memory:")
    profile = Profile()
    try:
        with profile.database(engine), profile.request("jobs"):
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                with pytest.raises(OperationalError):
                    connection.execute(text("SELECT * FROM missing_table"))
        sample = profile.samples[0]
        assert sample.statements == 2
        assert sample.sql_seconds > 0
        assert sample.partition()["database"] == pytest.approx(sample.sql_seconds * 1000)
        with profile.request("auth"), engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        assert profile.samples[1].statements == 0
    finally:
        engine.dispose()


def test_real_asgi_layers_and_thread_sql_add_up_per_concurrent_request():
    app = FastAPI()
    profile = Profile()
    engine = create_engine("sqlite:///:memory:")

    async def authenticate():
        with engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar_one()

    dependencies = SimpleNamespace(authenticate=authenticate)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Profile-Test"] = "yes"
        return response

    @app.get("/api/jobs")
    def jobs(user=Depends(authenticate)):
        with engine.connect() as connection:
            return {"count": connection.execute(text("SELECT 1")).scalar_one()}

    @app.get("/api/permissions")
    async def permissions(user=Depends(authenticate)):
        await asyncio.sleep(.001)
        return {"allowed": True}

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://odin.test") as client:
            async def one(path):
                with profile.request(category("GET", path)):
                    response = await client.get(path)
                assert response.status_code == 200
                assert response.headers["X-Profile-Test"] == "yes"
            await asyncio.gather(*(one(path) for path in ["/api/jobs", "/api/permissions"] * 5))

    try:
        original, replacement = profile.wrap(dependencies, "authenticate", "auth.user_resolution")
        install_framework(profile, app, {original: replacement})
        with profile.database(engine):
            asyncio.run(exercise())
        assert len(profile.samples) == 10
        for sample in profile.samples:
            layers = sample.partition()
            assert sum(layers.values()) == pytest.approx((sample.ended - sample.started) * 1000, rel=.05)
            assert layers["route_handler"] > 0
            assert layers["response_serialization"] > 0
            assert layers["middleware.headers"] > 0
            assert layers["auth.user_resolution"] > 0
            assert sample.sql_by_layer["auth.user_resolution"] > 0
            assert sample.statements == (2 if sample.category == "jobs" else 1)
        assert profile.table()["all"]["statements"] == 1.5
    finally:
        profile.restore()
        engine.dispose()
    assert app.middleware_stack is None
    assert app.routes[-1].dependant.call is permissions
    assert app.routes[-1].dependant.dependencies[0].call is authenticate


@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/api/auth/me", "auth"), ("GET", "/api/permissions", "auth"),
    ("GET", "/api/printers", "printers"), ("GET", "/api/jobs?limit=50", "jobs"),
    ("GET", "/api/quotas", "reports"), ("GET", "/api/education/usage-report?days=30", "reports"),
    ("POST", "/api/jobs", "job_create"), ("POST", "/api/jobs/10000/approve", "job_approve"),
    ("POST", "/api/auth/logout", "session_churn"),
])
def test_operation_classes_match_load_mix(method, path, expected):
    assert category(method, path) == expected


def test_summary_and_ranking_use_measured_tables_and_identify_noise():
    def result(values, sources):
        runs = [{"table": {"all": {"requests": 1000, "total_ms": sum(row.values()),
                 "layers_ms": row, "statements": 2, "sql_ms_inclusive": row["database"]}}} for row in values]
        return {"mean": summarize(runs), "sources": sources}
    before = result([{"database": value, "route_handler": 100} for value in (1, 2, 3)],
                    {"database": "SQLAlchemy", "route_handler": "route"})
    after = result([{"database": value, "route_handler": 102, "new": 5} for value in (2, 3, 4)],
                   {"database": "SQLAlchemy", "route_handler": "route", "new": "new_function"})
    comparison = compare(before, after)
    assert comparison["total_delta_ms"] == 8
    assert [row["layer"] for row in comparison["ranked"]] == ["new", "route_handler", "database"]
    assert comparison["ranked"][0]["new"]
    assert comparison["ranked"][1]["under_five_percent"]
    assert sum(row["percent_of_total_delta"] for row in comparison["ranked"]) == 100
    assert before["mean"]["all"]["total_range_ms"] == [101, 103]
    assert json.loads(json.dumps(comparison)) == comparison


def test_unrecognized_workload_operation_is_not_silently_auth():
    with pytest.raises(ValueError):
        category("GET", "/health")


def test_comparison_refuses_invalid_workloads():
    invalid = {"runs": [], "warmup": {"errors": [{"status": 500}], "tenant_leaks": 0}}
    with pytest.raises(ValueError, match="unexpected responses"):
        compare(invalid, invalid)


def test_text_and_json_include_every_class_and_spread():
    profile = Profile()
    profile.samples = [Sample(kind, 0, 1) for kind in CLASSES]
    runs = [{"table": profile.table(), "duration_seconds": 1, "errors": [], "tenant_leaks": 0} for _ in range(3)]
    result = {"tree": "test", "runs": runs, "mean": summarize(runs),
              "warmup": {"errors": [], "tenant_leaks": 0}, "notes": []}
    rendered = render_text(result)
    assert all(kind in rendered for kind in CLASSES)
    assert "Total range ms" in rendered
    assert "statements" in rendered
    assert json.loads(json.dumps(result))["mean"]["all"]["requests"] == 21


def test_standalone_isolation_blocks_network_and_environment_file_reads():
    # Exercise audit events directly: no real file is opened and no socket is made.
    code = '''
import os, sys
from ops.edu_readiness.request_profile import isolate
os.environ['DATABASE_URL'] = 'must-not-be-used'
isolate()
assert 'DATABASE_URL' not in os.environ
for event, args in [('socket.connect', ()), ('socket.getaddrinfo', ()),
                    ('socket.bind', ()), ('open', ('.env', 'r', 0)),
                    ('open', ('tests/.env.test', 'r', 0))]:
    try:
        sys.audit(event, *args)
    except RuntimeError:
        pass
    else:
        raise AssertionError(event)
'''
    child = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True)
    assert child.returncode == 0, child.stderr
