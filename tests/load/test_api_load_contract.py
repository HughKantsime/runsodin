import asyncio
import inspect
import json
from pathlib import Path

import pytest

from ops.edu_readiness import api_load
from ops.edu_readiness.api_load import _percentile, warmup_workload, workload


def test_workload_is_fixed_and_has_every_required_class():
    operations = workload(list(range(10000, 10075)), 0)
    assert len(operations) == 1000
    reads = [operation for operation in operations if operation.method == "GET"]
    writes = [operation for operation in operations if operation.method != "GET"]
    assert len(reads) == 800
    assert len(writes) == 200
    assert {operation.category for operation in operations} == {
        "auth", "printers", "jobs", "reports", "job_create", "job_approve", "session_churn"
    }


def test_warmup_covers_every_measured_class_before_latency_collection():
    operations = warmup_workload()
    assert len(operations) == 100
    assert {operation.category for operation in operations} == {
        "auth", "printers", "jobs", "reports", "job_create", "job_approve", "session_churn"
    }


def test_thresholds_are_complete_and_not_zero():
    path = Path(__file__).parents[2] / "ops" / "edu_readiness" / "thresholds.json"
    thresholds = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "warmup_operations", "repetitions", "concurrent_users", "operations_per_repetition",
        "minimum_reads", "minimum_writes", "maximum_repetition_seconds", "maximum_error_rate", "maximum_5xx",
        "maximum_tenant_leaks", "maximum_fd_delta", "maximum_memory_delta_mib",
        "websocket_cycles_per_repetition", "websocket_p95_ms",
    }
    assert required <= thresholds.keys()
    assert all(isinstance(thresholds[key], (int, float)) for key in required)
    assert thresholds["operations_per_repetition"] == 1000
    assert thresholds["concurrent_users"] == 25
    assert set(thresholds["host_classes"]) == {"mac-m4", "odin-ci"}
    for entry in thresholds["host_classes"].values():
        assert all(isinstance(entry[key], (int, float)) and entry[key] > 0 for key in api_load.LATENCY_LIMITS)
    # A host-neutral limit would apply on any machine, so none may exist.
    assert not thresholds.keys() & set(api_load.LATENCY_LIMITS)


def test_percentile_is_deterministic_and_empty_safe():
    assert _percentile([], 0.95) == 0.0
    assert _percentile([1, 2, 3, 4, 5], 0.95) == 4


def test_harness_uses_signed_ephemeral_license_not_validity_cache_bypass():
    source = inspect.getsource(api_load._prepare_database)
    assert "Ed25519PrivateKey.generate" in source
    assert "signing_key.sign" in source
    assert "verified_license = license_manager.get_license()" in source
    assert "LicenseInfo()" not in source
    assert "_cached_license = license_info" not in source


def test_rebaselined_limits_carry_their_previous_value_and_evidence():
    """2.0 re-baseline (Hugh, 2026-10-18), then the gate host's move to odin-ci (2026-10-30):
    only the latency limits changed, each with its previous value and evidence, and each
    record starts where the last one ended; every other limit is as before."""
    path = Path(__file__).parents[2] / "ops" / "edu_readiness" / "thresholds.json"
    thresholds = json.loads(path.read_text(encoding="utf-8"))
    first, latest = thresholds["rebaseline_2_0"], thresholds["rebaseline_odin_ci"]
    for record in (first, latest):
        changed = {key: value for key, value in record.items() if isinstance(value, dict)}
        assert set(changed) == {"read_p95_ms", "write_p95_ms", "global_p99_ms"}
        for entry in changed.values():
            assert entry["previous"] < entry["value"] and entry["evidence"]
    classes = thresholds["host_classes"]
    for key in api_load.LATENCY_LIMITS:
        assert latest[key]["previous"] == first[key]["value"]
        assert classes["mac-m4"][key] == first[key]["value"]
        assert classes["odin-ci"][key] == latest[key]["value"]
    assert {key: classes["mac-m4"][key] for key in api_load.LATENCY_LIMITS} == {
        "read_p95_ms": 650, "write_p95_ms": 1400, "global_p99_ms": 2800}
    assert {key: classes["odin-ci"][key] for key in api_load.LATENCY_LIMITS} == {
        "read_p95_ms": 1450, "write_p95_ms": 2550, "global_p99_ms": 3850}
    previous = {"maximum_repetition_seconds": 45, "maximum_error_rate": 0, "maximum_5xx": 0,
                "maximum_tenant_leaks": 0, "maximum_fd_delta": 8, "maximum_memory_delta_mib": 64,
                "websocket_p95_ms": 1000, "concurrent_users": 25, "operations_per_repetition": 1000,
                "minimum_reads": 800, "minimum_writes": 100, "repetitions": 3, "warmup_operations": 100}
    assert {key: thresholds[key] for key in previous} == previous


MAC = {"system": "Darwin", "machine": "arm64", "hostname": "ollamas-Mac-mini.local", "cpu": "Apple M4"}
ODIN_CI = {"system": "Linux", "machine": "x86_64", "hostname": "odin-ci",
           "cpu": "11th Gen Intel(R) Core(TM) i7-11700K @ 3.60GHz"}


def _thresholds():
    return json.loads(api_load.THRESHOLDS_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("facts,expected,limits", [
    (MAC, "mac-m4", {"read_p95_ms": 650, "write_p95_ms": 1400, "global_p99_ms": 2800}),
    (dict(MAC, hostname="renamed.local"), "mac-m4", {"read_p95_ms": 650, "write_p95_ms": 1400, "global_p99_ms": 2800}),
    (dict(MAC, hostname="odin-ci"), "mac-m4", {"read_p95_ms": 650, "write_p95_ms": 1400, "global_p99_ms": 2800}),  # no name gets the Mac the looser limits
    (ODIN_CI, "odin-ci", {"read_p95_ms": 1450, "write_p95_ms": 2550, "global_p99_ms": 3850}),
])
def test_each_known_host_gets_exactly_its_own_limits(facts, expected, limits):
    host_class, observed, identity = api_load.host_limits(_thresholds(), facts)
    assert (host_class, observed) == (expected, limits)
    assert identity == _thresholds()["host_classes"][expected]["identity"]


@pytest.mark.parametrize("facts", [
    dict(MAC, cpu="Apple M4 Pro"),
    dict(MAC, cpu="VirtualApple @ 2.50GHz", machine="x86_64"),  # Rosetta
    dict(MAC, cpu=""),
    dict(ODIN_CI, hostname="other-linux"),  # any other Linux box, however named
    dict(ODIN_CI, cpu="QEMU Virtual CPU"),  # a rebuilt VM on hardware nobody measured
    dict(ODIN_CI, machine="aarch64"),
    {},
])
def test_unknown_host_has_no_limits(facts):
    with pytest.raises(api_load.UnknownHostClass):
        api_load.host_limits(_thresholds(), facts)


def test_identity_free_or_ambiguous_class_table_refuses():
    thresholds = _thresholds()
    thresholds["host_classes"]["odin-ci"]["identity"] = {}  # would match every machine
    with pytest.raises(api_load.UnknownHostClass):
        api_load.host_limits(thresholds, dict(ODIN_CI))
    thresholds = _thresholds()
    thresholds["host_classes"]["copy"] = thresholds["host_classes"]["mac-m4"]
    with pytest.raises(api_load.UnknownHostClass):
        api_load.host_limits(thresholds, MAC)


def test_nothing_a_job_can_set_chooses_the_class(monkeypatch):
    """The class comes from the machine: an exported variable has no way in."""
    for name in ("ODIN_HOST_CLASS", "HOST_CLASS", "HOSTNAME", "RUNNER_NAME", "RUNNER_OS", "RUNNER_ARCH"):
        monkeypatch.setenv(name, "odin-ci")
    monkeypatch.setattr(api_load.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(api_load.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(api_load.socket, "gethostname", lambda: "ollamas-Mac-mini.local")
    monkeypatch.setattr(api_load, "_cpu_model", lambda: "Apple M4")
    assert api_load.host_limits(_thresholds(), api_load.host_facts())[0] == "mac-m4"
    source = inspect.getsource(api_load.host_facts) + inspect.getsource(api_load.host_limits)
    assert "os.environ" not in source and "getenv" not in source


RUN_ID = "20261030T000000Z-abcdef1"


def _passing_memory_pass(_environment):
    thresholds = _thresholds()
    cycles = thresholds["websocket_cycles_per_repetition"]
    metric = {
        "traced": True, "operations": 1000, "reads": 800, "writes": 200, "errors": 0, "unexpected_5xx": 0,
        "tenant_leaks": 0, "websocket_cycles": cycles, "websocket_failures": 0, "websocket_subscription_acks": cycles * 2,
        "websocket_messages_exchanged": cycles * 9, "websocket_isolation_checks": cycles, "websocket_cross_user_events": 0,
        "classes": dict.fromkeys(("auth", "printers", "jobs", "reports", "job_create", "job_approve", "session_churn"), 1),
    }
    return {"exit_code": 0, "failure": None, "duration_seconds": 1.0, "report": {
        "tracing": True, "memory_delta_bytes": 1024, "warmup_errors": 0, "warmup_tenant_leaks": 0, "integrity_check": "ok",
        "repetitions": [dict(metric, repetition=number + 1) for number in range(thresholds["repetitions"])],
    }}


def _stub_load(monkeypatch, milliseconds):
    """Everything real but the workload, which reports a chosen latency for every operation."""
    classes = ("auth", "printers", "jobs", "reports", "job_create", "job_approve", "session_churn")
    timings = {name: [float(milliseconds)] * (200 if name in ("auth", "printers", "jobs", "reports") else 67) for name in classes}
    timings["session_churn"] = timings["session_churn"][:66]

    async def execute(app, tokens, operations, concurrency):
        return timings, [], 0, 1.0

    def websocket(app, target, foreign, cycles):
        return {"timings": [1.0] * cycles, "failures": 0, "subscription_acks": cycles * 2,
                "messages_exchanged": cycles * 9, "isolation_checks": cycles, "cross_user_events": 0}

    monkeypatch.setattr(api_load, "_prepare_database", lambda path: (None, {
        "churn": ["x"] * 150, "ws_target": ["x"], "ws_foreign": ["x"], "viewer": ["x"]}))
    monkeypatch.setattr(api_load, "_execute_repetition", execute)
    monkeypatch.setattr(api_load, "_websocket_cycles", websocket)
    monkeypatch.setattr(api_load, "_run_memory_pass", _passing_memory_pass)


@pytest.mark.parametrize("facts,milliseconds,passes,host_class", [
    (MAC, 600, True, "mac-m4"),
    (MAC, 700, False, "mac-m4"),      # inside odin-ci's limits, over the Mac's: must not pass on the Mac
    (ODIN_CI, 1200, True, "odin-ci"),
    (ODIN_CI, 1500, False, "odin-ci"),
])
def test_gate_judges_a_run_by_its_own_host_and_records_both(monkeypatch, tmp_path, facts, milliseconds, passes, host_class):
    _stub_load(monkeypatch, milliseconds)
    monkeypatch.setattr(api_load, "host_facts", lambda: facts)
    output = tmp_path / "api_load.json"
    code = asyncio.run(api_load.run(RUN_ID, output))
    result = json.loads(output.read_text(encoding="utf-8"))
    expected = _thresholds()["host_classes"][host_class]
    assert (code == 0) is passes and (result["status"] == "pass") is passes, result["findings"]
    assert result["metrics"]["host_class"] == host_class
    assert result["metrics"]["latency_limits"] == {key: expected[key] for key in api_load.LATENCY_LIMITS}
    assert result["metrics"]["host_identity"] == expected["identity"]
    assert any("read p95" in finding for finding in result["findings"]) is not passes


def test_unknown_host_fails_the_gate_before_any_load(monkeypatch, tmp_path):
    def must_not_run(path):
        raise AssertionError("load ran without a limit to judge it")

    monkeypatch.setattr(api_load, "_prepare_database", must_not_run)
    monkeypatch.setattr(api_load, "_run_memory_pass", must_not_run)
    monkeypatch.setattr(api_load, "host_facts", lambda: dict(ODIN_CI, hostname="other-linux"))
    output = tmp_path / "api_load.json"
    assert asyncio.run(api_load.run(RUN_ID, output)) == 1
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "fail" and result["executed_count"] == 0
    assert result["metrics"]["host_class"] is None and result["metrics"]["latency_limits"] is None
    assert "no measured latency limits" in result["findings"][0]
    assert "other-linux" not in result["findings"][0]  # a stranger's hostname stays out of evidence
