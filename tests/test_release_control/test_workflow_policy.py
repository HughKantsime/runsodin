from pathlib import Path

from ops.release_control.policy import dispatch_allowed, load_inventory, valid_candidate, workflow_events, workflow_text

ROOT = Path(__file__).parents[2]


def test_WF01_only_manual_dispatch_is_subscribed():
    assert workflow_events(workflow_text()) == {"workflow_dispatch"}


def test_WF02_pull_request_event_is_absent():
    assert "pull_request:" not in workflow_text()


def test_WF03_push_event_is_absent():
    assert "\n  push:" not in workflow_text()


def test_WF04_other_automatic_events_are_absent():
    source = workflow_text()
    for event in ("schedule", "workflow_call", "repository_dispatch", "release", "deployment"):
        assert f"\n  {event}:" not in source


def test_WF05_permissions_are_fail_closed_and_job_read_only():
    source = workflow_text()
    assert "\npermissions: {}\n" in source
    assert "\n    permissions:\n      contents: read\n" in source
    assert "contents: write" not in source


def test_WF06_actor_mismatch_is_rejected():
    assert not dispatch_allowed("intruder", "201174638", "intruder", 1)
    assert 'test "$GITHUB_ACTOR" = "HughKantsime"' in workflow_text()
    assert "\n    if:" not in workflow_text()


def test_WF07_triggering_actor_mismatch_is_rejected():
    assert not dispatch_allowed("HughKantsime", "201174638", "intruder", 1)


def test_WF08_rerun_is_rejected():
    assert not dispatch_allowed("HughKantsime", "201174638", "HughKantsime", 2)
    assert 'test "$GITHUB_RUN_ATTEMPT" = "1"' in workflow_text()


def test_WF09_candidate_ref_shape_is_exact():
    sha = "a" * 40
    assert valid_candidate(sha, f"release-candidate/{sha}")
    assert not valid_candidate(sha, f"refs/heads/release-candidate/{sha}")


def test_WF10_candidate_sha_is_full_lowercase_hex():
    assert not valid_candidate("a" * 39, "release-candidate/" + "a" * 39)
    assert not valid_candidate("G" * 40, "release-candidate/" + "G" * 40)


def test_WF11_runner_labels_are_exact():
    assert "runs-on: [self-hosted, odin-isolated]" in workflow_text()
    assert "mac-mini-runner" not in workflow_text()


def test_WF12_workflow_has_no_release_side_effect_or_secret_surface():
    source = workflow_text()
    for forbidden in ("secrets.", "environment:", "docker push", "gh release", "git tag", "repository_dispatch"):
        assert forbidden not in source
    assert "make trusted-validation-gate PYTHON=python3.11" in source
    assert "fetch-depth: 0" in source
    assert "npm --prefix frontend ci" in source
    assert "python3.11 -m venv .hardware-cert-venv" in source
    assert "python3.11 -m venv .venv-stress" in source
    inventory = load_inventory()
    for component in inventory["components"]:
        assert component["working_directory"] == "."
        assert component["environment_allowlist"]
        assert component["expected_artifacts"]
        assert "timeout_seconds" in component
        assert component["source"]
