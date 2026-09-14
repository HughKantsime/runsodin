from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path

import pytest

from ops.hardware_certification import active_bambu, active_http, assets as certification_assets
from ops.hardware_certification.passive import live as passive_live
from ops.hardware_certification.active import ExerciseError, canonical_remote_name
from ops.hardware_certification.active_transports import (
    ActiveTransportError,
    BambuActiveTransport,
    ElegooActiveTransport,
    MoonrakerActiveTransport,
    PrusaLinkActiveTransport,
)
from ops.hardware_certification.assets import validate_test_asset
from ops.hardware_certification.security import SecurityError
from ops.hardware_certification.config import ResolvedTarget
from ops.hardware_certification.passive import transports as passive_transports


NONCE = "0123456789abcdef0123456789abcdef"


class _JsonResponse:
    status_code = 200
    is_redirect = False
    headers = {"Content-Type": "application/json", "Content-Encoding": "identity"}

    def close(self):
        return None

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield b"{}"


class _NoProxySession:
    instances: list["_NoProxySession"] = []

    def __init__(self):
        self.trust_env = True
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, *_args, **_kwargs):
        assert self.trust_env is False
        return _JsonResponse()

    def request(self, *_args, **_kwargs):
        assert self.trust_env is False
        return _JsonResponse()


def test_requests_transports_disable_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://public-proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://public-proxy.invalid:8080")
    monkeypatch.setattr(passive_transports.requests, "Session", _NoProxySession)
    passive = passive_transports.PinnedDigestHttpTransport(
        host="printer.cert.test", address="10.0.0.10", port=80,
        username="operator", password="secret",
    )
    assert passive.get("/api/version") == {}

    monkeypatch.setattr(active_http.requests, "Session", _NoProxySession)
    target = ResolvedTarget(
        "moonraker", "lab-1", "Voron", "10.0.0.10",
        {"host": "printer.cert.test", "port": 7125},
    )
    assert active_http._PinnedHttpMutator(target).moon_post("/printer/print/pause", "unused")
    assert all(instance.trust_env is False for instance in _NoProxySession.instances)


def test_bambu_partial_construction_preserves_interrupt_and_attempts_full_cleanup(monkeypatch):
    calls: list[str] = []

    class FaultingClient:
        def username_pw_set(self, *_args): return None
        def tls_set_context(self, *_args): return None
        def connect(self, *_args, **_kwargs): calls.append("connect")
        def loop_start(self):
            calls.append("loop_start")
            raise KeyboardInterrupt
        def disconnect(self):
            calls.append("disconnect")
            raise RuntimeError("disconnect failed")
        def loop_stop(self): calls.append("loop_stop")

    client = FaultingClient()
    monkeypatch.setattr(active_bambu.mqtt, "Client", lambda *_args, **_kwargs: client)
    target = ResolvedTarget(
        "bambu", "lab-1", "X1C", "127.0.0.1",
        {"host": "printer.cert.test", "port": 8883,
         "device_token": "ODIN-CERT-FICTIONAL-DEVICE",
         "access_code": "ODIN-CERT-FICTIONAL-ACCESS"},
    )
    with pytest.raises(KeyboardInterrupt):
        active_bambu.BambuLiveSession(target, "odin-cert.3mf", None)
    assert calls == ["connect", "loop_start", "disconnect", "loop_stop"]


def test_bambu_publish_preserves_primary_failure_and_attempts_full_cleanup(monkeypatch):
    calls: list[str] = []

    class FaultingSender:
        on_connect = None
        def username_pw_set(self, *_args): return None
        def tls_set_context(self, *_args): return None
        def connect(self, *_args, **_kwargs):
            calls.append("connect")
            self.on_connect(self, None, None, 0)
        def loop_start(self): calls.append("loop_start")
        def publish(self, *_args, **_kwargs):
            calls.append("publish")
            raise KeyboardInterrupt
        def disconnect(self):
            calls.append("disconnect")
            raise RuntimeError("disconnect failed")
        def loop_stop(self): calls.append("loop_stop")

    sender = FaultingSender()
    monkeypatch.setattr(active_bambu.mqtt, "Client", lambda *_args, **_kwargs: sender)
    session = active_bambu.BambuLiveSession.__new__(active_bambu.BambuLiveSession)
    session._BambuLiveSession__target = ResolvedTarget(
        "bambu", "lab-1", "X1C", "127.0.0.1",
        {"host": "printer.cert.test", "port": 8883,
         "device_token": "ODIN-CERT-FICTIONAL-DEVICE",
         "access_code": "ODIN-CERT-FICTIONAL-ACCESS"},
    )
    with pytest.raises(KeyboardInterrupt):
        session._BambuLiveSession__publish_closed({"print": {"command": "pause"}})
    assert calls == ["connect", "loop_start", "publish", "disconnect", "loop_stop"]


def test_bambu_close_attempts_loop_stop_when_disconnect_fails():
    calls: list[str] = []

    class FaultingClient:
        def disconnect(self):
            calls.append("disconnect")
            raise RuntimeError("disconnect failed")
        def loop_stop(self):
            calls.append("loop_stop")
            raise RuntimeError("loop stop failed")

    session = active_bambu.BambuLiveSession.__new__(active_bambu.BambuLiveSession)
    session._BambuLiveSession__client = FaultingClient()
    with pytest.raises(ExerciseError, match="cleanup failed"):
        session.close()
    assert calls == ["disconnect", "loop_stop"]


def test_elegoo_websocket_uses_preconnected_validated_peer_and_disables_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9999")
    sentinel = object()
    captured = {}
    frames = iter([
        '{"Topic":"sdcp/attributes/ODIN-CERT-FICTIONAL","Attributes":{"MachineName":"Centauri Carbon","ProtocolVersion":"V3.0.0","FirmwareVersion":"V1.0.0"}}',
        '{"Topic":"sdcp/status/ODIN-CERT-FICTIONAL","Status":{"CurrentStatus":[0],"PrintInfo":{"Status":0}}}',
        '{"Topic":"sdcp/status/ODIN-CERT-FICTIONAL","Status":{"CurrentStatus":[1],"PrintInfo":{"Status":1}}}',
    ])

    class WebSocket:
        def recv(self): return next(frames)
        def close(self): return None

    def tcp_connect(target, timeout):
        captured["tcp"] = (target, timeout)
        return sentinel

    monkeypatch.setattr(passive_live.socket, "create_connection", tcp_connect)

    def websocket_connect(_url, **options):
        captured.update(options)
        return WebSocket()

    monkeypatch.setattr(passive_live.websocket, "create_connection", websocket_connect)
    target = ResolvedTarget(
        "elegoo", "lab-1", "Centauri Carbon", "10.0.0.10",
        {"host": "printer.cert.test", "port": 3030},
    )
    summary = passive_live.observe_resolved_target(target)
    assert len(summary.samples) == 2
    assert captured["tcp"][0] == ("10.0.0.10", 3030)
    assert captured["socket"] is sentinel
    assert captured["http_proxy_host"] is None
    assert captured["http_no_proxy"] == ["10.0.0.10"]


def test_bambu_active_transport_emits_only_exact_certification_payloads():
    remote = canonical_remote_name("bambu", NONCE)
    published: list[dict] = []
    uploaded: list[str] = []
    transport = BambuActiveTransport(
        expected_remote_name=remote,
        publisher=lambda payload: published.append(payload) is None,
        uploader=lambda name: uploaded.append(name) is None,
        sequence=lambda: "12345",
    )
    assert transport.upload(remote)
    assert transport.start(remote)
    assert transport.pause()
    assert transport.resume()
    assert transport.stop()
    assert uploaded == [remote]
    assert [item["print"]["command"] for item in published] == ["project_file", "pause", "resume", "stop"]
    assert published[0]["print"]["url"] == f"ftp:///{remote}"
    assert not transport.start("operator-controlled.3mf")


def test_moonraker_active_transport_has_closed_paths_and_remote_name():
    remote = canonical_remote_name("moonraker", NONCE)
    paths: list[str] = []
    uploads: list[str] = []
    transport = MoonrakerActiveTransport(
        expected_remote_name=remote,
        poster=lambda path: paths.append(path) is None,
        uploader=lambda name: uploads.append(name) is None,
    )
    assert transport.upload(remote) and transport.start(remote)
    assert transport.pause() and transport.resume() and transport.cancel()
    assert uploads == [remote]
    assert paths == [
        f"/printer/print/start?filename={remote}",
        "/printer/print/pause", "/printer/print/resume", "/printer/print/cancel",
    ]


def test_prusalink_only_exposes_atomic_upload_start_and_numeric_job_paths():
    remote = canonical_remote_name("prusalink", NONCE)
    calls: list[tuple[str, object]] = []
    transport = PrusaLinkActiveTransport(
        expected_remote_name=remote,
        atomic_uploader=lambda name, data: calls.append((name, data)) is None,
        putter=lambda path: calls.append(("PUT", path)) is None,
        deleter=lambda path: calls.append(("DELETE", path)) is None,
    )
    assert not hasattr(transport, "upload") and not hasattr(transport, "start")
    assert transport.upload_start(remote)
    assert calls[0] == (remote, {"select": "true", "print": "true"})
    assert transport.pause(71) and transport.resume(71) and transport.stop(71)
    with pytest.raises(ActiveTransportError):
        transport.pause("71")


def test_elegoo_active_transport_uses_only_reviewed_sdcp_codes():
    frames: list[dict] = []
    fixed = uuid.UUID("00000000-0000-0000-0000-000000000001")
    transport = ElegooActiveTransport(
        mainboard_id="ODIN-CERT-FICTIONAL-MAINBOARD",
        sender=lambda frame: frames.append(frame) is None,
        uuid_factory=lambda: fixed, timestamp=lambda: 42,
    )
    assert transport.pause() and transport.resume() and transport.stop()
    assert [frame["Data"]["Cmd"] for frame in frames] == [129, 131, 130]
    assert all(frame["Topic"] == "sdcp/request/ODIN-CERT-FICTIONAL-MAINBOARD" for frame in frames)


def test_elegoo_active_observation_rejects_excessive_json_nesting():
    from ops.hardware_certification.active import ExerciseError
    from ops.hardware_certification.active_elegoo import ElegooLiveSession

    nested: object = "leaf"
    for _ in range(40):
        nested = {"next": nested}

    class Socket:
        def recv(self):
            return json.dumps({"Topic": "sdcp/status/ODIN-CERT-FICTIONAL", "extra": nested})

    session = ElegooLiveSession.__new__(ElegooLiveSession)
    session._ElegooLiveSession__socket = Socket()
    with pytest.raises(ExerciseError, match="observation failed"):
        session.observe()


def test_hardware_runtime_dependencies_are_exactly_pinned():
    requirements = Path("backend/requirements.txt").read_text(encoding="utf-8").splitlines()
    for dependency in (
        "jsonschema==4.25.1", "requests==2.33.1", "websocket-client==1.9.2",
        "websockets==15.0.1", "paho-mqtt==2.1.0", "defusedxml==0.7.1",
    ):
        assert dependency in requirements
    assert Path("ops/hardware_certification/requirements.txt").read_text(encoding="utf-8").splitlines()[-1] == "pytest==9.0.3"
    runner = Path("ops/hardware_certification/runner.py").read_text(encoding="utf-8")
    assert 'environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"' in runner
    makefile = Path("Makefile").read_text(encoding="utf-8")
    assert "HARDWARE_PYTHON ?=" in makefile
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=backend:. $(HARDWARE_PYTHON)" in makefile
    inventory = Path("ops/release_control/inventory.json").read_text(encoding="utf-8")
    assert '"make","test-hardware-certification","HARDWARE_PYTHON=.hardware-cert-venv/bin/python"' in inventory
    assert '"name":"hardware-certification"' in inventory
    assert ".hardware-cert-venv/" in Path(".gitignore").read_text(encoding="utf-8").splitlines()
    subprocess.run(
        ["git", "check-ignore", "-q", ".hardware-cert-venv/cleanliness-probe"],
        check=True,
    )


def test_test_asset_is_exact_hash_protected_and_outside_repository(tmp_path: Path, monkeypatch):
    repository = tmp_path / "repo"
    artifact_root = tmp_path / "artifacts"
    external = tmp_path / "operator" / "fixture.gcode"
    repository.mkdir(); artifact_root.mkdir(); external.parent.mkdir()
    external.write_bytes(b"; ODIN disposable certification fixture\n")
    external.chmod(0o600)
    digest = hashlib.sha256(external.read_bytes()).hexdigest()
    validated, extension = validate_test_asset(
        external, protocol="moonraker", expected_sha256=digest,
        repository_root=repository, artifact_root=artifact_root,
    )
    assert validated.path == external.resolve() and extension == ".gcode"
    with validated.open() as stream:
        assert stream.read().startswith(b"; ODIN")
    original = external.read_bytes()
    external.write_bytes(b"X" * len(original))
    with pytest.raises(SecurityError, match="content changed"):
        validated.open()
    external.write_bytes(original)
    with pytest.raises(SecurityError, match="SHA-256"):
        validate_test_asset(
            external, protocol="moonraker", expected_sha256="0" * 64,
            repository_root=repository, artifact_root=artifact_root,
        )
    inside = repository / "fixture.gcode"
    inside.write_bytes(b"fixture"); inside.chmod(0o600)
    with pytest.raises(SecurityError, match="outside"):
        validate_test_asset(
            inside, protocol="moonraker",
            expected_sha256=hashlib.sha256(inside.read_bytes()).hexdigest(),
            repository_root=repository, artifact_root=artifact_root,
        )
    canonical_artifacts = tmp_path / "canonical-artifacts"
    canonical_artifacts.mkdir()
    canonical_asset = canonical_artifacts / "fixture.gcode"
    canonical_asset.write_bytes(b"fixture"); canonical_asset.chmod(0o600)
    monkeypatch.setattr(certification_assets, "CANONICAL_ARTIFACT_ROOT", canonical_artifacts)
    with pytest.raises(SecurityError, match="outside"):
        validate_test_asset(
            canonical_asset, protocol="moonraker",
            expected_sha256=hashlib.sha256(canonical_asset.read_bytes()).hexdigest(),
            repository_root=repository, artifact_root=artifact_root,
        )
