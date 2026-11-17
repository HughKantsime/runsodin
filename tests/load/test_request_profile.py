"""Profiler accounting tests use only in-memory databases and in-process ASGI."""
import asyncio
import json
import subprocess
import sys
import time
from collections import Counter
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from ops.edu_readiness.request_profile import CLASSES, category, compare, install_framework, render_text, smoke_workload, summarize
from ops.edu_readiness.request_profile_layers import Profile, Sample, distribution, span, timed


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


@pytest.mark.parametrize("sequential", [False, True])
def test_real_asgi_layers_and_thread_sql_add_up_per_request(sequential):
    app = FastAPI()
    profile = Profile(sequential=sequential)
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
            paths = ["/api/jobs", "/api/permissions"] * 5
            if sequential:
                for path in paths:
                    await one(path)
            else:
                await asyncio.gather(*(one(path) for path in paths))

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
            if sequential:
                cpu = sample.partition(cpu=True)
                assert sum(cpu.values()) == pytest.approx((sample.cpu_ended - sample.cpu_started) * 1000, rel=.05)
                assert cpu["route_handler"] > 0
                assert cpu["database"] > 0
        assert profile.table()["all"]["statements"] == 1.5
    finally:
        profile.restore()
        engine.dispose()
    assert app.middleware_stack is None
    assert app.routes[-1].dependant.call is permissions
    assert app.routes[-1].dependant.dependencies[0].call is authenticate


def test_sequential_middleware_excludes_call_next_and_suspension():
    app = FastAPI()
    profile = Profile(sequential=True)
    waits = []

    @app.middleware("http")
    async def headers(request, call_next):
        started = time.perf_counter()
        await asyncio.sleep(.02)
        waits.append((time.perf_counter() - started) * 1000)
        response = await call_next(request)
        response.headers["X-Profile-Test"] = "yes"
        return response

    @app.get("/api/jobs")
    async def jobs():
        started = time.perf_counter()
        await asyncio.sleep(.02)
        waits.append((time.perf_counter() - started) * 1000)
        return {"count": sum(range(1000))}

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://odin.test") as client:
            with profile.request("jobs"):
                response = await client.get("/api/jobs")
            assert response.json() == {"count": 499500}
            assert response.headers["X-Profile-Test"] == "yes"

    try:
        install_framework(profile, app)
        asyncio.run(exercise())
        sample = profile.samples[0]
        layers = sample.partition()
        assert layers["middleware.headers"] < waits[0] * .25
        assert layers["route_handler"] < waits[1] * .25
        assert layers["framework_wait"] >= sum(waits) * .95
        assert sum(layers.values()) == pytest.approx((sample.ended - sample.started) * 1000, rel=.05)
    finally:
        profile.restore()


@pytest.mark.parametrize("sequential", [False, True])
def test_async_function_asgi_leaf_keeps_framework_work_out_of_middleware(sequential):
    profile = Profile(sequential=sequential)

    async def leaf(scope, receive, send):
        # A busy segment proves the leaf itself is awaited under a framework
        # span, even when it is a function rather than a callable class.
        end = time.process_time() + .005
        while time.process_time() < end:
            pass

    class Middleware:
        def __init__(self):
            self.app = leaf

        async def __call__(self, scope, receive, send):
            await self.app(scope, receive, send)

    async def exercise():
        with profile.request("auth"):
            await profile.middleware(Middleware())({}, None, None)

    try:
        asyncio.run(exercise())
        layers = profile.samples[0].partition()
        assert layers["framework_wait"] >= 5
        assert layers["middleware.Middleware"] < layers["framework_wait"] * .25
    finally:
        profile.restore()


def test_active_timing_preserves_cancellation_and_context():
    profile = Profile(sequential=True)
    finished = []

    async def blocked():
        try:
            await asyncio.sleep(10)
        finally:
            finished.append(True)

    async def exercise():
        with profile.request("auth"):
            task = asyncio.create_task(timed(blocked, "auth", True)())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        with span("outside_request"):
            pass

    asyncio.run(exercise())
    assert finished == [True]
    assert profile.samples[0].partition()["auth"] > 0
    assert "outside_request" not in profile.samples[0].partition()


def test_statement_trace_preserves_order_callers_and_omits_parameters():
    engine = create_engine("sqlite:///:memory:")
    profile = Profile(sequential=True)

    def issue_sql():
        with engine.connect() as connection:
            connection.execute(text("SELECT :value"), {"value": "synthetic-private-value"})
            with pytest.raises(OperationalError):
                connection.execute(text("SELECT * FROM missing_table"))

    try:
        with profile.database(engine), profile.request("jobs", statements=True):
            issue_sql()
        sample = profile.samples[0]
        assert [row["statement"] for row in sample.sql_trace] == ["SELECT ?", "SELECT * FROM missing_table"]
        assert all(row["source"].endswith("test_request_profile.py:issue_sql") for row in sample.sql_trace)
        assert sample.statements == len(sample.sql_trace)
        assert "synthetic-private-value" not in json.dumps(sample.sql_trace)
        with profile.database(engine), profile.request("jobs"):
            issue_sql()
        assert profile.samples[1].sql_trace is None
    finally:
        engine.dispose()


def test_sequential_distributions_use_requests_and_include_zero_layers():
    profile = Profile(sequential=True)
    samples = [Sample("jobs", 0, wall, [(0, wall / 2, "route_handler", 1)],
                      statements=index, sql_seconds=wall / 10, cpu_started=0, cpu_ended=wall / 4,
                      cpu_intervals=[(0, wall / 8, "route_handler", 1)])
               for index, wall in enumerate((1, 2, 10))]
    table = profile.distributions(samples)
    assert distribution([1, 2, 10]) == {"median": 2, "p10": 1.2, "p90": 8.4}
    assert table["all"]["wall_ms"] == {"median": 2000, "p10": 1200, "p90": 8400}
    assert table["jobs"]["cpu_ms"]["median"] == 500
    assert table["jobs"]["layers_wall_ms"]["route_handler"]["median"] == 1000
    assert table["jobs"]["layers_cpu_ms"]["route_handler"]["median"] == 250
    assert table["jobs"]["layers_wall_ms"]["database"] == dict.fromkeys(("median", "p10", "p90"), 0)


def test_smoke_selects_fifty_operations_with_proportional_class_coverage():
    from ops.edu_readiness.api_load import workload

    operations = workload(list(range(10000, 10075)), 0)
    smoke = smoke_workload(operations)
    assert len(smoke) == 50
    assert smoke == smoke_workload(operations)
    counts = Counter(operation.category for operation in smoke)
    assert set(counts) == set(CLASSES)
    for kind, count in Counter(operation.category for operation in operations).items():
        assert abs(counts[kind] - count / 20) <= 1
    assert len({op.path for op in smoke if op.category == "auth"}) == 2
    assert len({op.path for op in smoke if op.category == "reports"}) == 2


@pytest.mark.parametrize("smoke,expected", [(False, 3000), (True, 50)])
def test_sequential_runner_warms_then_measures_and_traces_without_timing_stack_walks(monkeypatch, smoke, expected):
    from ops.edu_readiness import api_load, request_profile, request_profile_layers

    engine = create_engine("sqlite:///:memory:")
    calls, active, maximum = [], 0, 0
    original_execute = api_load._execute_repetition
    caller_walks = []
    original_source = request_profile_layers.statement_source

    def caller():
        caller_walks.append(True)
        return original_source()

    async def response(client, method, url, *args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0)
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return httpx.Response(201 if method == "POST" and url == "/api/jobs" else 200, json={})
        finally:
            active -= 1

    async def execute(app, tokens, operations, concurrency):
        calls.append((len(operations), concurrency))
        return await original_execute(app, tokens, operations, concurrency)

    fixture = SimpleNamespace(
        _prepare_database=lambda path: (None, {kind: ["synthetic"] * 150 for kind in
                                              ("viewer", "admin", "operator", "churn", "warmup_churn")}),
        _execute_repetition=execute, warmup_workload=api_load.warmup_workload, workload=api_load.workload,
    )
    monkeypatch.setattr(request_profile, "load_workload", lambda tree: fixture)
    monkeypatch.setattr(request_profile, "install", lambda profile, app: None)
    monkeypatch.setitem(sys.modules, "core.db", SimpleNamespace(engine=engine))
    monkeypatch.setattr(request_profile.os, "environ", request_profile.os.environ.copy())
    monkeypatch.setattr(httpx.AsyncClient, "request", response)
    monkeypatch.setattr(request_profile_layers, "statement_source", caller)
    result = asyncio.run(request_profile.run(request_profile.ROOT, sequential=True, statements=True, smoke=smoke))
    assert calls == [(100, 1)] + ([(50, 1)] if smoke else [(1000, 1)] * 3)
    assert maximum == 1
    assert result["statistics"]["all"]["requests"] == expected
    assert result["statistics"]["all"]["statements"]["median"] == 1
    assert not result["warmup"]["errors"]
    assert all(not run["errors"] and not run["tenant_leaks"] for run in result["runs"])
    assert set(result["statement_requests"]) == set(CLASSES)
    assert len(caller_walks) == len(CLASSES)
    assert all(trace["phase"] == "warmup" and len(trace["statements"]) == 1
               for trace in result["statement_requests"].values())
    rendered = render_text(result)
    assert "Median [p10, p90]" in rendered
    assert "layers_cpu_ms.database" in rendered
    assert "SQL job_approve: POST" in rendered
    assert json.loads(json.dumps(result))["statistics"]["all"]["requests"] == expected
    assert httpx.AsyncClient.request is response


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
