"""Fail-loud publisher heartbeat contracts for the demo sandbox."""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SPEC = importlib.util.spec_from_file_location(
    "odin_demo_publisher", REPO_ROOT / "ops" / "demo" / "demo_publisher.py"
)
demo_publisher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(demo_publisher)


def test_heartbeat_write_creates_parent_and_is_parseable(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "publisher.heartbeat"
    monkeypatch.setattr(demo_publisher, "HEARTBEAT_PATH", path)
    demo_publisher._heartbeat()
    assert path.exists()
    assert float(path.read_text()) <= time.time()


@pytest.mark.parametrize("state", ["missing", "stale", "malformed"])
def test_heartbeat_probe_fails_for_unhealthy_state(tmp_path, state):
    path = tmp_path / "publisher.heartbeat"
    if state == "stale":
        path.write_text(str(time.time() - 300))
    elif state == "malformed":
        path.write_text("not-a-timestamp")

    ok, _message = demo_publisher.check_heartbeat(path, max_stale_sec=90)
    assert ok is False


def test_heartbeat_probe_accepts_fresh_file(tmp_path):
    path = tmp_path / "publisher.heartbeat"
    path.write_text(str(time.time()))
    ok, message = demo_publisher.check_heartbeat(path, max_stale_sec=90)
    assert ok is True
    assert "age=" in message


def test_heartbeat_write_failure_is_not_swallowed(tmp_path, monkeypatch):
    path = tmp_path / "blocked"
    path.mkdir()
    monkeypatch.setattr(demo_publisher, "HEARTBEAT_PATH", path)
    with pytest.raises(OSError):
        demo_publisher._heartbeat()
