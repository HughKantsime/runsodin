from __future__ import annotations

import hashlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ops.hardware_certification import active
from ops.hardware_certification.active import (
    Observation, canonical_remote_name, execute_authorized, run_exercise,
)
from ops.hardware_certification.active_bambu import BambuLiveSession
from ops.hardware_certification.active_elegoo import ElegooLiveSession
from ops.hardware_certification.active_http import MoonrakerLiveSession, PrusaLinkLiveSession
from ops.hardware_certification.assets import validate_test_asset
from ops.hardware_certification.authorization import AuthorizationError, create_template
from ops.hardware_certification.config import ResolvedTarget
from ops.hardware_certification.passive.observers import MOON_QUERY
from ops.hardware_certification.passive.parsers import parse_moonraker_sample
from ops.hardware_certification.simulators import (
    MiniImplicitFtpsServer, MiniTlsMqttBroker, StatefulElegooPeer,
    StatefulHardwareHttpPeer,
)
from ops.hardware_certification.security import SecurityError


NONCE = "0123456789abcdef0123456789abcdef"
CORRELATION_KEY = "ab" * 32


class SimulatedBackend:
    def __init__(self, protocol: str, *, initial: Observation | None = None, fail_action: str | None = None):
        self.protocol = protocol
        self.current = initial or Observation("idle")
        self.fail_action = fail_action
        self.calls: list[tuple[str, object]] = []

    def close(self):
        self.calls.append(("close", None))

    def _ok(self, action: str, value: object = None) -> bool:
        self.calls.append((action, value))
        return action != self.fail_action

    def observe(self):
        self.calls.append(("observe", None))
        return self.current

    def upload(self, remote_name):
        return self._ok("upload", remote_name)

    def start(self, remote_name):
        if not self._ok("start", remote_name): return False
        self.current = Observation("printing", remote_name, "job-cert")
        return True

    def upload_start(self, remote_name):
        if not self._ok("upload_start", remote_name): return False
        self.current = Observation("printing", remote_name, 71)
        return True

    def pause(self, job_id):
        if not self._ok("pause", job_id): return False
        self.current = Observation("paused", self.current.filename, self.current.job_id)
        return True

    def resume(self, job_id):
        if not self._ok("resume", job_id): return False
        self.current = Observation("printing", self.current.filename, self.current.job_id)
        return True

    def stop(self, job_id):
        if not self._ok("stop", job_id): return False
        self.current = Observation("stopped", self.current.filename, self.current.job_id)
        return True

    def cancel(self, job_id):
        if not self._ok("cancel", job_id): return False
        self.current = Observation("stopped", self.current.filename, self.current.job_id)
        return True

    def read_ams(self):
        self.calls.append(("ams_read", None))
        return 4


@pytest.mark.parametrize(
    "protocol,actions,terminal",
    [
        ("bambu", ["upload", "start", "pause", "resume", "ams_read", "stop"], "stopped"),
        ("moonraker", ["upload", "start", "pause", "resume", "cancel"], "stopped"),
        ("prusalink", ["upload_start", "pause", "resume", "stop"], "stopped"),
    ],
)
def test_same_run_active_simulator_workflows(protocol, actions, terminal):
    backend = SimulatedBackend(protocol)
    results = run_exercise(protocol, NONCE, actions, backend)
    assert results == [{"action": action, "status": "pass"} for action in actions]
    assert backend.current.state == terminal
    assert backend.current.filename == active.canonical_remote_name(protocol, NONCE)


def _validated_asset(tmp_path: Path, protocol: str):
    extension = ".gcode" if protocol in {"moonraker", "prusalink"} else ".3mf"
    path = tmp_path / f"ODIN-CERT-FICTIONAL-ASSET{extension}"
    path.write_bytes(b"ODIN deterministic disposable test asset\n")
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    asset, _extension = validate_test_asset(
        path, protocol=protocol, expected_sha256=digest,
        repository_root=Path.cwd(), artifact_root=Path.cwd() / "artifacts",
    )
    return asset


def test_prusalink_bgcode_asset_is_explicitly_rejected(tmp_path: Path):
    path = tmp_path / "ODIN-CERT-FICTIONAL-ASSET.bgcode"
    path.write_bytes(b"ODIN deterministic disposable test asset\n")
    path.chmod(0o600)
    with pytest.raises(SecurityError, match="extension is not supported"):
        validate_test_asset(
            path, protocol="prusalink",
            expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            repository_root=Path.cwd(), artifact_root=Path.cwd() / "artifacts",
        )


def _moon_status(state: str, filename: str = "") -> dict:
    return {"result": {"status": {"print_stats": {
        "state": state, "filename": filename,
    }}}}


def _prusa_status(state: str, filename: str = "", job_id: int | None = None) -> tuple[dict, dict]:
    return (
        {"printer": {"state": state}},
        {"job": {"id": job_id, "file": {"name": filename}}},
    )


def test_moonraker_live_session_runs_real_parser_and_http_action_workflow(tmp_path: Path):
    remote_name = canonical_remote_name("moonraker", NONCE)
    asset = _validated_asset(tmp_path, "moonraker")
    with StatefulHardwareHttpPeer(
        "moonraker", remote_name, asset.path.read_bytes(),
    ) as peer:
        port = int(peer.base_url.rsplit(":", 1)[1])
        target = ResolvedTarget(
            "moonraker", "lab-1", "Voron", "127.0.0.1",
            {"host": "printer.cert.test", "port": port},
        )
        backend = MoonrakerLiveSession(
            target, remote_name, asset,
        ).backend()
        results = run_exercise(
            "moonraker", NONCE, ["upload", "start", "pause", "resume", "cancel"], backend,
        )
        backend.close()
    assert all(item["status"] == "pass" for item in results), results
    assert peer.command_errors == []
    assert [request for request in peer.requests if request[0] == "POST"] == [
        ("POST", "/server/files/upload"),
        ("POST", f"/printer/print/start?filename={remote_name}"),
        ("POST", "/printer/print/pause"),
        ("POST", "/printer/print/resume"),
        ("POST", "/printer/print/cancel"),
    ]


def test_prusalink_live_session_runs_real_parser_and_http_action_workflow(tmp_path: Path):
    remote_name = canonical_remote_name("prusalink", NONCE)
    asset = _validated_asset(tmp_path, "prusalink")
    with StatefulHardwareHttpPeer(
        "prusalink", remote_name, asset.path.read_bytes(),
    ) as peer:
        port = int(peer.base_url.rsplit(":", 1)[1])
        target = ResolvedTarget(
            "prusalink", "lab-1", "CORE One", "127.0.0.1",
            {"host": "printer.cert.test", "port": port,
             "api_key": "ODIN-CERT-FICTIONAL-KEY"},
        )
        backend = PrusaLinkLiveSession(
            target, remote_name, asset,
        ).backend()
        results = run_exercise(
            "prusalink", NONCE, ["upload_start", "pause", "resume", "stop"], backend,
        )
        backend.close()
    assert all(item["status"] == "pass" for item in results), results
    assert peer.command_errors == []
    assert [request for request in peer.requests if request[0] != "GET"] == [
        ("POST", "/api/files/local"),
        ("PUT", "/api/v1/job/71/pause"),
        ("PUT", "/api/v1/job/71/resume"),
        ("DELETE", "/api/v1/job/71"),
    ]


def _elegoo_frame(state: int, print_state: int, filename: str) -> str:
    return json.dumps({
        "Topic": "sdcp/status/ODIN-CERT-FICTIONAL-MAINBOARD",
        "Status": {"CurrentStatus": [state], "PrintInfo": {
            "Status": print_state, "Filename": filename,
        }},
    }, separators=(",", ":"))


def test_elegoo_live_session_runs_real_parser_and_websocket_action_workflow():
    filename = "ODIN-CERT-MANUAL-FICTIONAL.ctb"
    mainboard = "ODIN-CERT-FICTIONAL-MAINBOARD"
    with StatefulElegooPeer(mainboard, filename) as peer:
        port = int(peer.url.rsplit(":", 1)[1])
        target = ResolvedTarget(
            "elegoo", "lab-1", "Centauri Carbon", "127.0.0.1",
            {"host": "printer.cert.test", "port": port,
             "mainboard_id": mainboard},
        )
        backend = ElegooLiveSession(target).backend()
        salt = "abcdef0123456789abcdef0123456789"
        results = run_exercise(
            "elegoo", NONCE, ["pause", "resume", "stop"], backend,
            elegoo_filename_salt=salt,
            elegoo_filename_sha256=hashlib.sha256((salt + filename).encode()).hexdigest(),
        )
        backend.close()
        server_thread = peer._thread
        handler_threads = tuple(peer._handler_threads)
    assert all(item["status"] == "pass" for item in results), results
    assert peer.command_errors == []
    commands = [json.loads(frame)["Data"]["Cmd"] for frame in peer.client_frames]
    assert commands == [129, 131, 130]
    assert server_thread is not None and not server_thread.is_alive()
    assert handler_threads and all(not thread.is_alive() for thread in handler_threads)


def test_bambu_live_session_runs_real_v2_parser_mqtt_and_ftps_workflow(tmp_path: Path):
    remote_name = canonical_remote_name("bambu", NONCE)
    def reports(state: str, count: int, start: int) -> list[dict]:
        return [{"print": {
            "gcode_state": state,
            "gcode_file": remote_name if state != "IDLE" else "",
            "subtask_id": "job-cert" if state != "IDLE" else "",
            "mc_percent": start + index, "msg": start + index,
            "ams": {"ams": []},
        }} for index in range(count)]

    initial = reports("IDLE", 2, 1)
    transitions = {
        "project_file": reports("RUNNING", 2, 10),
        "pause": reports("PAUSE", 2, 20),
        "resume": reports("RUNNING", 3, 30),
        "stop": reports("IDLE", 1, 40),
    }
    token = "ODIN-CERT-FICTIONAL-DEVICE"
    asset = _validated_asset(tmp_path, "bambu")
    with MiniImplicitFtpsServer(remote_name, asset.path.read_bytes()) as ftps, MiniTlsMqttBroker(
        token, initial, active_remote_name=remote_name,
        command_reports=transitions,
        command_sequence=["project_file", "pause", "resume", "stop"],
    ) as broker:
        target = ResolvedTarget(
            "bambu", "lab-1", "X1C", broker.host,
            {"host": broker.host, "port": broker.port, "device_token": token,
             "access_code": "ODIN-CERT-FICTIONAL-ACCESS", "ftps_port": ftps.port},
        )
        session = BambuLiveSession(target, remote_name, asset)
        backend = session.backend()
        results = run_exercise(
            "bambu", NONCE, ["upload", "start", "pause", "resume", "ams_read", "stop"], backend,
        )
        backend.close()
        ftps_server_thread = ftps._thread
        ftps_handler_threads = ftps._handler_threads
    assert all(item["status"] == "pass" for item in results), (
        results, ftps.commands, ftps.command_errors, ftps.received_name,
        ftps.received_data, broker.command_errors,
    )
    assert ftps.received_name == remote_name
    assert ftps.received_data == asset.path.read_bytes()
    assert ftps.command_errors == []
    assert ftps_server_thread is not None and not ftps_server_thread.is_alive()
    assert ftps_handler_threads and all(not thread.is_alive() for thread in ftps_handler_threads)
    assert broker.command_errors == []
    assert [payload["print"]["command"] for _topic, payload in broker.published_messages] == [
        "project_file", "pause", "resume", "stop",
    ]
    assert all(topic == f"device/{token}/request" for topic, _payload in broker.published_messages)
    assert broker.unexpected_packet_types == []


def test_moonraker_real_parser_idle_state_can_enter_active_upload_workflow():
    parsed = parse_moonraker_sample({
        "result": {"status": {"print_stats": {"state": "standby", "filename": ""}}},
    })
    backend = SimulatedBackend("moonraker", initial=Observation(parsed.state))
    assert run_exercise("moonraker", NONCE, ["upload", "start"], backend) == [
        {"action": "upload", "status": "pass"},
        {"action": "start", "status": "pass"},
    ]


def test_elegoo_requires_preauthorized_exact_filename_for_every_command():
    filename = "ODIN-CERT-MANUAL-FICTIONAL.ctb"
    salt = "abcdef0123456789abcdef0123456789"
    digest = hashlib.sha256((salt + filename).encode()).hexdigest()
    backend = SimulatedBackend("elegoo", initial=Observation("printing", filename))
    actions = ["pause", "resume", "stop"]
    assert run_exercise(
        "elegoo", NONCE, actions, backend,
        elegoo_filename_salt=salt, elegoo_filename_sha256=digest,
    ) == [{"action": action, "status": "pass"} for action in actions]


def test_identity_mismatch_blocks_before_command():
    backend = SimulatedBackend("elegoo", initial=Observation("printing", "someone-elses-job.ctb"))
    results = run_exercise(
        "elegoo", NONCE, ["pause", "resume"], backend,
        elegoo_filename_salt="a" * 32, elegoo_filename_sha256="b" * 64,
    )
    assert results == [{"action": "pause", "status": "fail"}]
    assert not any(call[0] == "pause" for call in backend.calls)


@pytest.mark.parametrize("changed", [Observation("paused", "other.gcode", "job-cert"), Observation("paused", active.canonical_remote_name("bambu", NONCE), None)])
def test_pause_fails_if_same_run_identity_changes_after_command(changed):
    class IdentityChangingBackend(SimulatedBackend):
        def pause(self, job_id):
            if not self._ok("pause", job_id):
                return False
            self.current = changed
            return True

    backend = IdentityChangingBackend("bambu")
    results = run_exercise("bambu", NONCE, ["upload", "start", "pause", "stop"], backend)
    assert results[-1] == {"action": "pause", "status": "fail"}
    assert not any(call[0] == "stop" for call in backend.calls)


def test_first_failure_stops_sequence():
    backend = SimulatedBackend("bambu", fail_action="upload")
    results = run_exercise("bambu", NONCE, ["upload", "start", "pause"], backend)
    assert results == [{"action": "upload", "status": "fail"}]
    assert not any(call[0] in {"start", "pause"} for call in backend.calls)


def test_stop_requires_a_safe_terminal_state(monkeypatch):
    class UnsafeTerminalBackend(SimulatedBackend):
        def stop(self, job_id):
            self._ok("stop", job_id)
            self.current = Observation("printing", self.current.filename, self.current.job_id)
            return True

    monkeypatch.setattr(active, "TRANSITION_TIMEOUT_SECONDS", 0.0)
    backend = UnsafeTerminalBackend("bambu")
    results = run_exercise("bambu", NONCE, ["upload", "start", "stop"], backend)
    assert results[-1] == {"action": "stop", "status": "fail"}


def test_active_transition_polls_through_reviewed_intermediate_states(monkeypatch):
    class DelayedStartBackend(SimulatedBackend):
        def start(self, remote_name):
            if not self._ok("start", remote_name):
                return False
            self.pending = [
                Observation("preparing", remote_name, "job-cert"),
                Observation("printing", remote_name, "job-cert"),
            ]
            return True

        def observe(self):
            self.calls.append(("observe", None))
            if getattr(self, "pending", None):
                self.current = self.pending.pop(0)
            return self.current

    monkeypatch.setattr(active.time, "sleep", lambda _seconds: None)
    backend = DelayedStartBackend("bambu")
    assert run_exercise("bambu", NONCE, ["upload", "start"], backend)[-1] == {
        "action": "start", "status": "pass",
    }


def test_active_transition_timeout_fails_closed(monkeypatch):
    class NeverStartsBackend(SimulatedBackend):
        def start(self, remote_name):
            if not self._ok("start", remote_name):
                return False
            self.current = Observation("preparing", remote_name, "job-cert")
            return True

    monkeypatch.setattr(active, "TRANSITION_TIMEOUT_SECONDS", 0.0)
    backend = NeverStartsBackend("bambu")
    results = run_exercise("bambu", NONCE, ["upload", "start"], backend)
    assert results[-1] == {"action": "start", "status": "fail"}


def test_active_dispatch_is_literal_and_has_no_reflection():
    source = inspect.getsource(active)
    assert "getattr(" not in source
    assert "importlib" not in source
    assert set(active.DISPATCH) == {
        (protocol, action) for protocol, actions in active.ACTION_TABLE.items() for action in actions
    }


def test_live_active_sessions_do_not_expose_generic_send_or_publish_methods():
    root = Path("ops/hardware_certification")
    for name in ("active_bambu.py", "active_elegoo.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "def publish(" not in source
        assert "def send(" not in source
        assert "__import__(" not in source
        assert "self.socket" not in source


def test_backend_is_constructed_only_after_authorization_is_consumed(tmp_path):
    target = {
        "schema_version": 1, "protocol": "prusalink", "target_alias": "lab-printer",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "CORE", "connection": {
            "host": "printer.cert.test", "port": 443,
            "api_key": "ODIN-CERT-FICTIONAL-KEY",
        },
    }
    target_path = tmp_path / "target.json"
    target_path.write_text(json.dumps(target)); target_path.chmod(0o600)
    auth = create_template(
        "20260912T120000Z-7d46cf1", target_path, ["upload_start"],
        test_asset_sha256="a" * 64,
        now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
    )
    auth.update(
        authorization_state="AUTHORIZED", operator_confirmation=auth["challenge"],
        operator_name="Test Operator", physical_area_clear=True,
        disposable_job_confirmed=True, emergency_stop_ready=True,
    )
    auth["actions"][0]["approved"] = True
    auth_path = tmp_path / "authorization.json"
    auth_path.write_text(json.dumps(auth)); auth_path.chmod(0o600)

    def factory(_target, _authorization):
        assert not auth_path.exists(), "backend was constructed before authorization consumption"
        return SimulatedBackend("prusalink")

    assert execute_authorized(
        authorization_path=auth_path, target_path=target_path,
        ledger_path=tmp_path / "state" / "used", backend_factory=factory,
        now=datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc),
    ) == [{"action": "upload_start", "status": "pass"}]


def test_consumed_authorization_uses_the_already_validated_target_snapshot(tmp_path, monkeypatch):
    target = {
        "schema_version": 1, "protocol": "prusalink", "target_alias": "lab-printer",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "CORE", "connection": {
            "host": "printer.cert.test", "port": 443,
            "api_key": "ODIN-CERT-FICTIONAL-KEY",
        },
    }
    target_path = tmp_path / "target.json"
    target_path.write_text(json.dumps(target)); target_path.chmod(0o600)
    auth = create_template(
        "20260912T120000Z-7d46cf1", target_path, ["upload_start"],
        test_asset_sha256="a" * 64,
        now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
    )
    auth.update(
        authorization_state="AUTHORIZED", operator_confirmation=auth["challenge"],
        operator_name="Test Operator", physical_area_clear=True,
        disposable_job_confirmed=True, emergency_stop_ready=True,
    )
    auth["actions"][0]["approved"] = True
    auth_path = tmp_path / "authorization.json"
    auth_path.write_text(json.dumps(auth)); auth_path.chmod(0o600)
    real_consume = active.consume_authorization

    def consume_then_swap(*args, **kwargs):
        consumed = real_consume(*args, **kwargs)
        target["target_alias"] = "swapped-target"
        target["connection"]["host"] = "other.cert.test"
        target_path.write_text(json.dumps(target)); target_path.chmod(0o600)
        return consumed

    monkeypatch.setattr(active, "consume_authorization", consume_then_swap)

    def factory(validated_target, _authorization):
        assert validated_target["target_alias"] == "lab-printer"
        assert validated_target["connection"]["host"] == "printer.cert.test"
        return SimulatedBackend("prusalink")

    assert execute_authorized(
        authorization_path=auth_path, target_path=target_path,
        ledger_path=tmp_path / "state" / "used", backend_factory=factory,
        now=datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc),
    ) == [{"action": "upload_start", "status": "pass"}]


def test_invalid_authorization_never_constructs_backend(tmp_path):
    target = {
        "schema_version": 1, "protocol": "bambu", "target_alias": "lab-printer",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "X1", "connection": {"host": "printer.cert.test", "port": 8883,
        "device_token": "ODIN-CERT-FICTIONAL-DEVICE", "access_code": "ODIN-CERT-FICTIONAL-ACCESS"},
    }
    target_path = tmp_path / "target.json"; target_path.write_text(json.dumps(target)); target_path.chmod(0o600)
    auth = create_template("20260912T120000Z-7d46cf1", target_path, ["pause"])
    auth_path = tmp_path / "authorization.json"; auth_path.write_text(json.dumps(auth)); auth_path.chmod(0o600)
    called = False

    def factory(_target, _authorization):
        nonlocal called
        called = True
        return SimulatedBackend("bambu")

    with pytest.raises(AuthorizationError):
        execute_authorized(
            authorization_path=auth_path, target_path=target_path,
            ledger_path=tmp_path / "state" / "used", backend_factory=factory,
        )
    assert called is False
