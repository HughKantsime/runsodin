from __future__ import annotations

from pathlib import Path

import pytest

from ops.hardware_certification import telemetry_prerequisites as prerequisites
from ops.hardware_certification.evidence import EvidenceError


def _result(mode: str, assertions: list[dict], status: str = "pass") -> dict:
    return {
        "mode": mode, "protocol": "bambu", "status": status,
        "assertions": assertions, "metrics": {
            "model_family": "X1C", "target_correlation_sha256": "c" * 64,
        },
    }


def test_v2_prerequisites_map_only_verified_action_evidence_and_keep_soak_blocked(tmp_path: Path, monkeypatch):
    observe = tmp_path / "observe"; exercise = tmp_path / "exercise"
    observe.mkdir(); exercise.mkdir()
    (observe / "manifest.json").write_bytes(b"observe manifest")
    (exercise / "manifest.json").write_bytes(b"exercise manifest")
    observe_result = _result("observe", [{"id": "two_valid_samples", "status": "pass"}])
    exercise_result = _result("exercise", [
        {"id": f"action_{name}", "status": "pass"}
        for name in ("upload", "start", "pause", "resume", "stop", "ams_read")
    ])

    def verify(path, **_kwargs):
        result = observe_result if path == observe else exercise_result
        return {"status": "pass"}, {"bambu": result}

    monkeypatch.setattr(prerequisites, "verify_artifact", verify)
    monkeypatch.setattr(prerequisites, "git_identity", lambda: ("7d46cf1", False))
    output = tmp_path / "telemetry-v2-prerequisites.json"
    payload = prerequisites.build_prerequisites(
        observe_artifact=observe, exercise_artifact=exercise, output=output,
    )
    assert payload["prerequisites"]["live_status"]["status"] == "pass"
    assert payload["prerequisites"]["pause_resume_stop"]["status"] == "pass"
    assert payload["prerequisites"]["ams_sync"]["status"] == "blocked"
    assert payload["prerequisites"]["dispatch"]["status"] == "pass"
    assert payload["prerequisites"]["database_alert_transitions"] == {
        "status": "blocked", "source_artifact_sha256": None,
    }
    assert payload["prerequisites"]["seven_day_staging_soak"]["status"] == "blocked"
    assert (output.stat().st_mode & 0o777) == 0o600


def test_missing_active_evidence_never_infers_command_or_dispatch_pass(tmp_path: Path, monkeypatch):
    observe = tmp_path / "observe"; observe.mkdir()
    (observe / "manifest.json").write_bytes(b"observe manifest")
    monkeypatch.setattr(
        prerequisites, "verify_artifact",
        lambda *_args, **_kwargs: ({"status": "pass"}, {"bambu": _result("observe", [], "pass")}),
    )
    monkeypatch.setattr(prerequisites, "git_identity", lambda: ("7d46cf1", False))
    payload = prerequisites.build_prerequisites(
        observe_artifact=observe, output=tmp_path / "result.json",
    )
    for name in ("pause_resume_stop", "ams_sync", "dispatch"):
        assert payload["prerequisites"][name] == {
            "status": "blocked", "source_artifact_sha256": None,
        }


@pytest.mark.parametrize("mismatch", ["model_family", "target_correlation_sha256"])
def test_unrelated_observe_and_exercise_evidence_cannot_be_combined(tmp_path: Path, monkeypatch, mismatch):
    observe = tmp_path / "observe"; exercise = tmp_path / "exercise"
    observe.mkdir(); exercise.mkdir()
    (observe / "manifest.json").write_bytes(b"observe manifest")
    (exercise / "manifest.json").write_bytes(b"exercise manifest")
    observe_result = _result("observe", [])
    exercise_result = _result("exercise", [])
    exercise_result["metrics"][mismatch] = "P1S" if mismatch == "model_family" else "d" * 64

    def verify(path, **_kwargs):
        return {"status": "pass"}, {"bambu": observe_result if path == observe else exercise_result}

    monkeypatch.setattr(prerequisites, "verify_artifact", verify)
    monkeypatch.setattr(prerequisites, "git_identity", lambda: ("7d46cf1", False))
    with pytest.raises(EvidenceError, match="identity is incoherent"):
        prerequisites.build_prerequisites(
            observe_artifact=observe, exercise_artifact=exercise,
            output=tmp_path / "result.json",
        )
