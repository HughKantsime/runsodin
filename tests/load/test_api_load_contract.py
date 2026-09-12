import inspect
import json
from pathlib import Path

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
        "minimum_reads", "minimum_writes", "maximum_repetition_seconds", "read_p95_ms",
        "write_p95_ms", "global_p99_ms", "maximum_error_rate", "maximum_5xx",
        "maximum_tenant_leaks", "maximum_fd_delta", "maximum_memory_delta_mib",
        "websocket_cycles_per_repetition", "websocket_p95_ms",
    }
    assert required <= thresholds.keys()
    assert all(isinstance(thresholds[key], (int, float)) for key in required)
    assert thresholds["operations_per_repetition"] == 1000
    assert thresholds["concurrent_users"] == 25


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
