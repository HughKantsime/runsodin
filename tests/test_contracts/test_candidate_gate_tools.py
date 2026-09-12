from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ops.release_gate.policy import (
    GatePolicyError,
    assert_candidate_suite_has_no_skip_mechanisms,
    inspect_junit,
    scan_text_for_secrets,
)
from ops.release_gate.report import render_report
from ops.release_gate import runner
from ops.release_gate.runner import ResourceNames, validate_run_id


def _write_junit(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "run_id",
    [
        "20260912t171500z-a1b2c3d4",
        "ci-1234-aabbccdd",
        "local-odin-42",
    ],
)
def test_run_id_and_resource_names_are_bounded(run_id: str):
    assert validate_run_id(run_id) == run_id
    names = ResourceNames.from_run_id(run_id)
    assert names.container == f"odin-candidate-{run_id}"
    assert names.network == f"odin-candidate-{run_id}"
    assert names.volume == f"odin-candidate-{run_id}-data"
    assert all(name.startswith("odin-candidate-") for name in names.all)


@pytest.mark.parametrize(
    "run_id",
    ["", "../../prod", "odin candidate", "UPPER", "a" * 65, "candidate;docker-rm"],
)
def test_run_id_rejects_unsafe_or_ambiguous_values(run_id: str):
    with pytest.raises(GatePolicyError):
        validate_run_id(run_id)


def test_default_run_ids_are_unique_for_same_commit_and_second(monkeypatch):
    fixed = runner.datetime(2026, 9, 12, 18, 50, 0, tzinfo=runner.timezone.utc)

    class FrozenDateTime:
        @classmethod
        def now(cls, _timezone):
            return fixed

    entropy = iter(("aabbccdd", "11223344"))
    monkeypatch.setattr(runner, "datetime", FrozenDateTime)
    monkeypatch.setattr(runner, "_git", lambda *_args: "01d14a18")
    monkeypatch.setattr(runner.secrets, "token_hex", lambda _bytes: next(entropy))

    first = runner._default_run_id()
    second = runner._default_run_id()
    assert first == "20260912t185000z-01d14a18-aabbccdd"
    assert second == "20260912t185000z-01d14a18-11223344"
    assert first != second


def test_junit_policy_accepts_only_nonempty_clean_report(tmp_path: Path):
    report = _write_junit(
        tmp_path / "pass.xml",
        '<testsuite tests="2" failures="0" errors="0" skipped="0">'
        '<testcase classname="candidate" name="one" />'
        '<testcase classname="candidate" name="two" />'
        "</testsuite>",
    )
    totals = inspect_junit(report)
    assert totals == {"tests": 2, "passed": 2, "failures": 0, "errors": 0, "skipped": 0}


@pytest.mark.parametrize(
    ("filename", "xml"),
    [
        ("empty.xml", '<testsuite tests="0" failures="0" errors="0" skipped="0" />'),
        (
            "skip.xml",
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase name="hidden"><skipped type="pytest.xfail">expected failure</skipped></testcase>'
            "</testsuite>",
        ),
        (
            "failure.xml",
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase name="hidden"><failure>boom</failure></testcase>'
            "</testsuite>",
        ),
        (
            "error.xml",
            '<testsuites><testsuite tests="1"><testcase name="hidden"><error>boom</error>'
            "</testcase></testsuite></testsuites>",
        ),
        (
            "attribute-failure.xml",
            '<testsuite tests="1" failures="1" errors="0" skipped="0">'
            '<testcase name="looks-clean" /></testsuite>',
        ),
        (
            "attribute-error.xml",
            '<testsuites tests="1" failures="0" errors="1" skipped="0">'
            '<testsuite tests="1"><testcase name="looks-clean" /></testsuite></testsuites>',
        ),
        (
            "attribute-skip.xml",
            '<testsuite tests="1" failures="0" errors="0" skipped="1">'
            '<testcase name="looks-clean" /></testsuite>',
        ),
        (
            "test-count-mismatch.xml",
            '<testsuite tests="2" failures="0" errors="0" skipped="0">'
            '<testcase name="only-one" /></testsuite>',
        ),
        (
            "invalid-total.xml",
            '<testsuite tests="many"><testcase name="only-one" /></testsuite>',
        ),
        (
            "negative-total.xml",
            '<testsuite tests="1" failures="-1"><testcase name="only-one" /></testsuite>',
        ),
    ],
)
def test_junit_policy_rejects_zero_hidden_skip_failure_or_error(
    tmp_path: Path, filename: str, xml: str
):
    with pytest.raises(GatePolicyError):
        inspect_junit(_write_junit(tmp_path / filename, xml))


def test_secret_scanner_rejects_generated_and_common_secret_shapes():
    generated = ["temporary-admin-password-123"]
    clean = "candidate ready; 12 tests passed; image sha256:abc123"
    assert scan_text_for_secrets(clean, generated) == []

    samples = {
        "known": "temporary-admin-password-123",
        "bearer": "Authorization: Bearer abc.def.ghi",
        "api": "API_KEY=not-for-an-artifact",
        "password": "ODIN_ADMIN_PASSWORD=not-for-an-artifact",
        "pem": "-----BEGIN PRIVATE KEY-----",
    }
    for label, value in samples.items():
        assert scan_text_for_secrets(value, generated), label


def test_log_redaction_removes_derived_jwt_before_retention():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjYW5kaWRhdGUifQ.signaturebytes"
    raw = f'WebSocket /api/v1/ws?token={token} [accepted]'
    redacted = runner.redact_text(raw, ())
    assert token not in redacted
    assert "[REDACTED]" in redacted
    assert scan_text_for_secrets(redacted, ()) == []


@pytest.mark.parametrize(
    "source",
    [
        "import pytest\npytest.skip('no')\n",
        "import pytest\npytest.xfail('no')\n",
        "import pytest\npytestmark = pytest.mark.skip\n",
        "from unittest import skip\n@skip('no')\ndef test_x(): pass\n",
        "def pytest_ignore_collect(path): return True\n",
        "collect_ignore = ['test_real.py']\n",
    ],
)
def test_candidate_suite_policy_rejects_skip_xfail_and_deselection(
    tmp_path: Path, source: str
):
    suite = tmp_path / "candidate_gate"
    suite.mkdir()
    (suite / "test_policy.py").write_text(source, encoding="utf-8")
    with pytest.raises(GatePolicyError):
        assert_candidate_suite_has_no_skip_mechanisms(suite)


def test_candidate_suite_policy_accepts_plain_deterministic_tests(tmp_path: Path):
    suite = tmp_path / "candidate_gate"
    suite.mkdir()
    (suite / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    assert_candidate_suite_has_no_skip_mechanisms(suite)


def test_html_report_is_truthful_and_contains_no_secret_values(tmp_path: Path):
    manifest = {
        "run_id": "ci-123-aabbccdd",
        "status": "FAIL",
        "commit": "aabbccdd",
        "dirty": False,
        "candidate_image_id": "sha256:one",
        "running_image_id": "sha256:one",
        "phases": [
            {"name": "api", "status": "PASS", "detail": "8 tests"},
            {"name": "browser", "status": "FAIL", "detail": "1 failure"},
        ],
        "fixtures": {"users": 3, "printers": 1},
    }
    output = tmp_path / "index.html"
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    screenshot_dir = tmp_path / "playwright"
    screenshot_dir.mkdir()
    (screenshot_dir / "failure.png").write_bytes(b"not-a-real-png")
    render_report(manifest, output)
    html = output.read_text(encoding="utf-8")
    assert "FAIL" in html
    assert "browser" in html
    assert "1 failure" in html
    assert "temporary-admin-password-123" not in html
    assert json.dumps(manifest)[0] not in html[:20]
    assert 'href="manifest.json"' in html
    assert 'href="playwright/failure.png"' in html


@pytest.mark.parametrize("existing_kind", ["container", "network", "volume"])
def test_candidate_preflight_rejects_preexisting_docker_resources(monkeypatch, existing_kind: str):
    resources = ResourceNames.from_run_id("ci-preexisting-aabbccdd")
    calls: list[list[str]] = []

    def fake_command(args, **_kwargs):
        calls.append(args)
        kind = args[1]
        resource_name = getattr(resources, kind)
        output = resource_name + "\n" if kind == existing_kind else ""
        return subprocess.CompletedProcess(args, 0, output)

    monkeypatch.setattr(runner, "_command", fake_command)
    with pytest.raises(GatePolicyError, match="already exist"):
        runner._assert_resources_absent(resources)
    assert calls
    assert all("inspect" not in args for args in calls)
    assert all("--format" in args for args in calls)


def test_candidate_make_and_ci_wiring_is_release_blocking():
    repo = Path(__file__).parents[2]
    makefile = (repo / "Makefile").read_text(encoding="utf-8")
    workflow = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    gitignore = (repo / ".gitignore").read_text(encoding="utf-8")

    assert "test-candidate:" in makefile
    assert "$(CANDIDATE_PYTHON) -m ops.release_gate.runner" in makefile
    assert "candidate-full-stack:" in workflow
    assert "runs-on: mac-mini-runner" in workflow
    assert "make test-candidate CANDIDATE_PYTHON=python3.11" in workflow
    assert "if: always()" in workflow
    assert "artifacts/candidate-gate/**" in workflow
    assert "docker push" not in workflow
    assert "artifacts/candidate-gate/" in gitignore
    requirements = (repo / "tests" / "requirements-test.txt").read_text(encoding="utf-8")
    assert "defusedxml==0.7.1" in requirements


def test_candidate_pytest_is_cut_off_from_legacy_parent_conftest():
    repo = Path(__file__).parents[2]
    source = (repo / "ops" / "release_gate" / "runner.py").read_text(encoding="utf-8")
    assert source.count('"--confcutdir=tests/candidate_gate"') == 2


def test_candidate_readiness_retries_transient_remote_disconnect(monkeypatch):
    from http.client import RemoteDisconnected

    attempts = iter((RemoteDisconnected("starting"), (200, {"ready": True})))

    def fake_request(*_args, **_kwargs):
        result = next(attempts)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(runner, "_loopback_json_request", fake_request)
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)
    runner._wait_ready("http://127.0.0.1:43210", timeout_seconds=1)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:8000",
        "http://localhost:8000",
        "http://example.com:8000",
        "http://user@127.0.0.1:8000",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1",
    ],
)
def test_candidate_http_helper_rejects_non_loopback_origin(base_url: str):
    with pytest.raises(GatePolicyError, match="candidate base URL"):
        runner._loopback_json_request(base_url, "/health/ready")
