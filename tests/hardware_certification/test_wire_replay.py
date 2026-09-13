from __future__ import annotations

import socket
import urllib.error
from contextlib import contextmanager

import pytest

from ops.hardware_certification.config import ResolvedTarget
from ops.hardware_certification.passive import live as passive_live
from ops.hardware_certification.passive.live import observe_resolved_target
from ops.hardware_certification.passive.observers import MOON_QUERY, ObservationError, observe_elegoo
from ops.hardware_certification.passive.transports import (
    PassivePolicyError,
    PinnedReadOnlyHttpTransport,
    ReadOnlyHttpTransport,
)
from ops.hardware_certification.simulators import (
    HttpReply, MiniJsonHttpPeer, MiniTlsMqttBroker, MiniWebSocketPeer,
)


def _state_sequence(protocol: str, final_state: str):
    first_printing = final_state == "idle"
    if protocol == "bambu":
        states = ("RUNNING", "IDLE") if first_printing else ("IDLE", "RUNNING")
        return [
            {"print": {"gcode_state": state, "mc_percent": index * 10,
                       "printer_type": "BL-P001", "msg": index}}
            for index, state in enumerate(states, start=1)
        ]
    if protocol == "elegoo":
        states = (1, 0) if first_printing else (0, 1)
        return [
            ('{"Topic":"sdcp/status/ODIN-CERT-FICTIONAL","Status":'
             f'{{"CurrentStatus":[{state}],"PrintInfo":{{"Status":{state},'
             f'"CurrentTicks":{index},"Filename":"ODIN-CERT-FICTIONAL.ctb"}}}}}}')
            for index, state in enumerate(states, start=1)
        ]
    if protocol == "moonraker":
        states = ("printing", "standby") if first_printing else ("standby", "printing")
        return [
            {"result": {"eventtime": float(index), "status": {"print_stats": {
                "state": state, "filename": "ODIN-CERT-FICTIONAL.gcode",
                "print_duration": float(index),
            }}}}
            for index, state in enumerate(states, start=1)
        ]
    states = ("PRINTING", "IDLE") if first_printing else ("IDLE", "PRINTING")
    return [
        ({"printer": {"state": state}},
         {"job": {"id": 7, "time_printing": index,
                  "file": {"name": "ODIN-CERT-FICTIONAL.gcode"}}})
        for index, state in enumerate(states, start=1)
    ]


@contextmanager
def _protocol_peer(protocol: str, final_state: str, failure_mode: str = "none"):
    samples = _state_sequence(protocol, final_state)
    if protocol == "bambu":
        token = "ODIN-CERT-FICTIONAL-DEVICE"
        sessions = None
        reports = samples
        if failure_mode == "disconnect":
            reports = samples[:1]
        elif failure_mode == "reconnect":
            sessions = [samples[:1], samples]
        with MiniTlsMqttBroker(token, reports, session_reports=sessions) as peer:
            yield ResolvedTarget(
                "bambu", "lab-1", "X1C", peer.host,
                {"host": peer.host, "port": peer.port, "device_token": token,
                 "access_code": "ODIN-CERT-FICTIONAL-ACCESS"},
            ), peer
        return
    if protocol == "elegoo":
        sessions = None
        attributes = (
            '{"Topic":"sdcp/attributes/ODIN-CERT-FICTIONAL",'
            '"Attributes":{"MachineName":"Centauri Carbon",'
            '"ProtocolVersion":"V3.0.0","FirmwareVersion":"V1.0.0"}}'
        )
        frames = [attributes, *samples]
        if failure_mode == "disconnect":
            frames = [attributes, *samples[:1]]
        elif failure_mode == "reconnect":
            sessions = [[attributes, *samples[:1]], [attributes, *samples]]
        with MiniWebSocketPeer(frames, session_frames=sessions) as peer:
            port = int(peer.url.rsplit(":", 1)[1])
            yield ResolvedTarget(
                "elegoo", "lab-1", "Centauri Carbon", "127.0.0.1",
                {"host": "printer.cert.test", "port": port},
            ), peer
        return
    disconnects = [HttpReply(disconnect=True), HttpReply(disconnect=True)]
    if protocol == "moonraker":
        info: object = {"result": {"moonraker_version": "ODIN-CERT-1"}}
        if failure_mode == "disconnect":
            info = disconnects
        elif failure_mode == "reconnect":
            info = [HttpReply(disconnect=True), info]
        responses = {
            "/server/info": info,
            "/printer/objects/list": {"result": {"objects": ["print_stats"]}},
            MOON_QUERY: samples,
        }
        model = "Voron"
        connection = {"host": "printer.cert.test"}
    else:
        version: object = {
            "api": "ODIN-CERT-1", "version": "ODIN-CERT-1",
            "printer": "ODIN-CERT-PRINTER-SOFTWARE-1",
            "firmware": "ODIN-CERT-FIRMWARE-1",
        }
        if failure_mode == "disconnect":
            version = disconnects
        elif failure_mode == "reconnect":
            version = [HttpReply(disconnect=True), version]
        responses = {
            "/api/version": version,
            "/api/v1/status": [sample[0] for sample in samples],
            "/api/job": [sample[1] for sample in samples],
        }
        model = "CORE One"
        connection = {"host": "printer.cert.test", "api_key": "ODIN-CERT-FICTIONAL-KEY"}
    with MiniJsonHttpPeer(responses) as peer:
        connection["port"] = int(peer.base_url.rsplit(":", 1)[1])
        yield ResolvedTarget(protocol, "lab-1", model, "127.0.0.1", connection), peer


@pytest.mark.parametrize("protocol", ["bambu", "elegoo", "moonraker", "prusalink"])
def test_protocol_live_worker_proves_idle_state(protocol):
    with _protocol_peer(protocol, "idle") as (target, _peer):
        summary = observe_resolved_target(target)
    assert summary.samples[-1].state == "idle"


@pytest.mark.parametrize("protocol", ["bambu", "elegoo", "moonraker", "prusalink"])
def test_protocol_live_worker_proves_printing_state(protocol):
    with _protocol_peer(protocol, "printing") as (target, _peer):
        summary = observe_resolved_target(target)
    assert summary.samples[-1].state == "printing"


def test_http_replay_peer_terminates_server_and_handler_threads():
    with MiniJsonHttpPeer({"/server/info": {"result": {"moonraker_version": "ODIN-CERT-1"}}}) as peer:
        ReadOnlyHttpTransport(peer.base_url, "moonraker").get("/server/info")
        server_thread = peer._thread
        handler_threads = peer._handler_threads
    assert server_thread is not None and not server_thread.is_alive()
    assert handler_threads and all(not thread.is_alive() for thread in handler_threads)


@pytest.mark.parametrize("protocol", ["bambu", "elegoo", "moonraker", "prusalink"])
def test_protocol_disconnect_fails_after_bounded_retry(protocol, monkeypatch):
    monkeypatch.setattr(passive_live, "OBSERVATION_TIMEOUT", 0.1)
    with _protocol_peer(protocol, "printing", "disconnect") as (target, peer):
        with pytest.raises(ObservationError, match="passive_(samples_unavailable|transport_unavailable)"):
            observe_resolved_target(target)
        if hasattr(peer, "connection_count"):
            assert peer.connection_count == 2


@pytest.mark.parametrize("protocol", ["bambu", "elegoo", "moonraker", "prusalink"])
def test_protocol_reconnect_succeeds_once_and_records_provenance(protocol, monkeypatch):
    monkeypatch.setattr(passive_live, "OBSERVATION_TIMEOUT", 0.1)
    with _protocol_peer(protocol, "printing", "reconnect") as (target, peer):
        summary = observe_resolved_target(target)
        assert summary.reconnect_count == 1
        if hasattr(peer, "connection_count"):
            assert peer.connection_count == 2


def test_bambu_tls_mqtt_replay_runs_live_passive_worker_and_v2_parser(monkeypatch):
    token = "ODIN-CERT-FICTIONAL-DEVICE"
    clients = []
    client_threads = []
    real_client = passive_live.mqtt.Client

    def recording_client(*args, **kwargs):
        client = real_client(*args, **kwargs)
        real_loop_stop = client.loop_stop

        def recording_loop_stop(*loop_args, **loop_kwargs):
            if client._thread is not None:
                client_threads.append(client._thread)
            return real_loop_stop(*loop_args, **loop_kwargs)

        client.loop_stop = recording_loop_stop
        clients.append(client)
        return client

    monkeypatch.setattr(passive_live.mqtt, "Client", recording_client)
    reports = [
        {"print": {"gcode_state": "IDLE", "mc_percent": 0, "printer_type": "BL-P001"}},
        {"print": {"gcode_state": "RUNNING", "stg_cur": 14, "mc_percent": 25, "printer_type": "BL-P001"}},
    ]
    with MiniTlsMqttBroker(token, reports) as broker:
        target = ResolvedTarget(
            protocol="bambu", target_alias="lab-1", model_family="X1C",
            address=broker.host, connection={
                "host": broker.host, "port": broker.port, "device_token": token,
                "access_code": "ODIN-CERT-FICTIONAL-ACCESS",
            },
        )
        summary = observe_resolved_target(target)
        assert [sample.state for sample in summary.samples] == ["idle", "printing"]
        assert summary.observed_model_family == "X1C"
        assert summary.freshness_seconds > 0
        assert broker.subscriptions == [f"device/{token}/report"]
        assert broker.authenticated is True
        assert broker.unexpected_packet_types == []
        assert clients and clients[0]._thread is None
        assert client_threads and all(not thread.is_alive() for thread in client_threads)
        broker_thread = broker._thread
        handler_threads = tuple(broker._handler_threads)
    assert broker_thread is not None and not broker_thread.is_alive()
    assert handler_threads and all(not thread.is_alive() for thread in handler_threads)


def test_moonraker_loopback_peer_runs_live_passive_worker_with_exact_gets():
    responses = {
        "/server/info": {"result": {"moonraker_version": "ODIN-CERT-1"}},
        "/printer/objects/list": {"result": {"objects": ["print_stats"]}},
        MOON_QUERY: [
            {"result": {"eventtime": 1.0, "status": {
                "print_stats": {"state": "standby", "filename": "ODIN-CERT-FICTIONAL.gcode"},
            }}},
            {"result": {"eventtime": 1.1, "status": {
                "print_stats": {"state": "printing", "filename": "ODIN-CERT-FICTIONAL.gcode"},
            }}},
        ],
    }
    with MiniJsonHttpPeer(responses) as peer:
        port = int(peer.base_url.rsplit(":", 1)[1])
        summary = observe_resolved_target(ResolvedTarget(
            "moonraker", "lab-1", "Voron", "127.0.0.1",
            {"host": "printer.cert.test", "port": port},
        ))
    assert [sample.state for sample in summary.samples] == ["idle", "printing"]
    assert summary.observed_model_family is None
    assert peer.requests == [
        ("GET", "/server/info"), ("GET", "/printer/objects/list"),
        ("GET", MOON_QUERY), ("GET", MOON_QUERY),
    ]


def test_prusalink_loopback_peer_runs_live_passive_worker_with_exact_gets():
    responses = {
        "/api/version": {
            "api": "ODIN-CERT-1", "version": "ODIN-CERT-1",
            "printer": "ODIN-CERT-PRINTER-SOFTWARE-1",
            "firmware": "ODIN-CERT-FIRMWARE-1",
        },
        "/api/v1/status": [
            {"printer": {"state": "IDLE"}, "telemetry_sequence": 1},
            {"printer": {"state": "PRINTING"}, "telemetry_sequence": 2},
        ],
        "/api/job": [
            {"job": {"id": 7, "file": {"name": "ODIN-CERT-FICTIONAL.gcode"}}},
            {"job": {"id": 7, "file": {"name": "ODIN-CERT-FICTIONAL.gcode"}, "progress": 1}},
        ],
    }
    with MiniJsonHttpPeer(responses) as peer:
        port = int(peer.base_url.rsplit(":", 1)[1])
        summary = observe_resolved_target(ResolvedTarget(
            "prusalink", "lab-1", "CORE One", "127.0.0.1",
            {"host": "printer.cert.test", "port": port, "api_key": "ODIN-CERT-FICTIONAL-KEY"},
    ))
    assert [sample.state for sample in summary.samples] == ["idle", "printing"]
    assert summary.observed_model_family is None
    assert summary.firmware_version == "ODIN-CERT-FIRMWARE-1"
    assert peer.requests == [
        ("GET", "/api/version"),
        ("GET", "/api/v1/status"), ("GET", "/api/job"),
        ("GET", "/api/v1/status"), ("GET", "/api/job"),
    ]
    assert all(api_key for _path, api_key, _authorization in peer.auth_headers)


def test_elegoo_loopback_peer_runs_live_passive_worker_without_client_frames(monkeypatch):
    sockets = []
    real_create_connection = passive_live.websocket.create_connection

    def recording_create_connection(*args, **kwargs):
        client = real_create_connection(*args, **kwargs)
        sockets.append(client)
        return client

    monkeypatch.setattr(passive_live.websocket, "create_connection", recording_create_connection)
    frames = [
        '{"Topic":"sdcp/attributes/ODIN-CERT-FICTIONAL","Attributes":{"MachineName":"Centauri Carbon","ProtocolVersion":"V3.0.0","FirmwareVersion":"V1.0.0"}}',
        '{"Topic":"sdcp/status/ODIN-CERT-FICTIONAL","Status":{"CurrentStatus":[0],"PrintInfo":{"Status":0,"Filename":"ODIN-CERT-FICTIONAL.ctb"}}}',
        '{"Topic":"sdcp/status/ODIN-CERT-FICTIONAL","Status":{"CurrentStatus":[1],"PrintInfo":{"Status":1,"Filename":"ODIN-CERT-FICTIONAL.ctb"}}}',
    ]
    with MiniWebSocketPeer(frames) as peer:
        port = int(peer.url.rsplit(":", 1)[1])
        summary = observe_resolved_target(ResolvedTarget(
            "elegoo", "lab-1", "Centauri Carbon", "127.0.0.1",
            {"host": "printer.cert.test", "port": port},
        ))
    assert [sample.state for sample in summary.samples] == ["idle", "printing"]
    assert summary.observed_model_family == "Centauri Carbon"
    assert summary.firmware_version == "V1.0.0"
    assert summary.api_version == "V3.0.0"
    assert peer.client_frames == []
    assert sockets and sockets[0].connected is False
    assert peer._thread is not None and not peer._thread.is_alive()
    assert peer._handler_threads
    assert all(not thread.is_alive() for thread in peer._handler_threads)


def test_bambu_and_elegoo_disconnect_before_two_samples_fail_closed(monkeypatch):
    monkeypatch.setattr(passive_live, "OBSERVATION_TIMEOUT", 0.1)
    token = "ODIN-CERT-FICTIONAL-DEVICE"
    with MiniTlsMqttBroker(token, [{"print": {"gcode_state": "IDLE"}}]) as broker:
        target = ResolvedTarget(
            "bambu", "lab-1", "X1C", broker.host,
            {"host": broker.host, "port": broker.port, "device_token": token,
             "access_code": "ODIN-CERT-FICTIONAL-ACCESS"},
        )
        with pytest.raises(ObservationError, match="passive_samples_unavailable"):
            observe_resolved_target(target)

    frame = '{"Topic":"sdcp/status/ODIN-CERT-FICTIONAL","Status":{"CurrentStatus":[0],"PrintInfo":{"Status":0}}}'
    with MiniWebSocketPeer([frame]) as peer:
        port = int(peer.url.rsplit(":", 1)[1])
        target = ResolvedTarget(
            "elegoo", "lab-1", "Centauri Carbon", "127.0.0.1",
            {"host": "printer.cert.test", "port": port},
        )
        with pytest.raises(ObservationError, match="passive_samples_unavailable"):
            observe_resolved_target(target)


def test_bambu_authentication_failure_is_bounded(monkeypatch):
    monkeypatch.setattr(passive_live, "OBSERVATION_TIMEOUT", 0.1)
    token = "ODIN-CERT-FICTIONAL-DEVICE"
    with MiniTlsMqttBroker(token, [], access_code="DIFFERENT-CODE") as broker:
        target = ResolvedTarget(
            "bambu", "lab-1", "X1C", broker.host,
            {"host": broker.host, "port": broker.port, "device_token": token,
             "access_code": "ODIN-CERT-FICTIONAL-ACCESS"},
        )
        with pytest.raises(ObservationError, match="passive_samples_unavailable"):
            observe_resolved_target(target)
        assert broker.authenticated is False


def test_prusalink_authentication_failure_is_bounded():
    responses = {"/api/version": HttpReply(status=401)}
    with MiniJsonHttpPeer(responses) as peer:
        port = int(peer.base_url.rsplit(":", 1)[1])
        target = ResolvedTarget(
            "prusalink", "lab-1", "CORE One", "127.0.0.1",
            {"host": "printer.cert.test", "port": port,
             "api_key": "ODIN-CERT-FICTIONAL-KEY"},
        )
        with pytest.raises(ObservationError):
            observe_resolved_target(target)
        assert peer.auth_headers == [("/api/version", True, False)]


@pytest.mark.parametrize(
    ("protocol", "path"),
    [("moonraker", "/server/info"), ("prusalink", "/api/version")],
    ids=["moonraker", "prusalink"],
)
def test_http_protocol_oversize_and_unavailable_endpoints_fail_closed(protocol, path):
    with MiniJsonHttpPeer({path: HttpReply(body=b'{"padding":"' + b"x" * 256 + b'"}')}) as peer:
        with pytest.raises(PassivePolicyError, match="size"):
            ReadOnlyHttpTransport(peer.base_url, protocol, max_response_bytes=64).get(path)
    with MiniJsonHttpPeer({}) as peer:
        with pytest.raises(urllib.error.HTTPError):
            ReadOnlyHttpTransport(peer.base_url, protocol).get(path)


def test_bambu_and_elegoo_oversize_payloads_fail_closed(monkeypatch):
    monkeypatch.setattr(passive_live, "OBSERVATION_TIMEOUT", 0.1)
    token = "ODIN-CERT-FICTIONAL-DEVICE"
    oversized = {"print": {"gcode_state": "IDLE", "padding": "x" * 263_000}}
    with MiniTlsMqttBroker(token, [oversized, oversized]) as broker:
        target = ResolvedTarget(
            "bambu", "lab-1", "X1C", broker.host,
            {"host": broker.host, "port": broker.port, "device_token": token,
             "access_code": "ODIN-CERT-FICTIONAL-ACCESS"},
        )
        with pytest.raises(ObservationError, match="passive_samples_unavailable"):
            observe_resolved_target(target)

    frame = '{"Topic":"sdcp/status/ODIN-CERT-FICTIONAL","padding":"' + "x" * 263_000 + '"}'
    transport = type("Transport", (), {"receive": lambda self: frame})()
    with pytest.raises(ObservationError, match="payload_size_invalid"):
        observe_elegoo(transport)


def test_http_passive_wire_rejects_redirect_malformed_oversize_and_auth():
    cases = {
        "/server/info": HttpReply(status=302, headers={"Location": "http://127.0.0.1/elsewhere"}),
        "/printer/info": HttpReply(body=b"not-json"),
        "/printer/objects/list": HttpReply(body=b'{"padding":"' + b"x" * 256 + b'"}'),
        "/printer/objects/query?print_stats": HttpReply(status=401),
    }
    with MiniJsonHttpPeer(cases) as peer:
        transport = ReadOnlyHttpTransport(peer.base_url, "moonraker", max_response_bytes=64)
        with pytest.raises(PassivePolicyError, match="redirect"):
            transport.get("/server/info")
        with pytest.raises(PassivePolicyError, match="malformed"):
            transport.get("/printer/info")
        with pytest.raises(PassivePolicyError, match="size"):
            transport.get("/printer/objects/list")
        with pytest.raises(urllib.error.HTTPError):
            transport.get("/printer/objects/query?print_stats")
    assert all(method == "GET" for method, _path in peer.requests)


def test_http_passive_wire_rejects_excessive_json_nesting():
    nested = '"leaf"'
    for _ in range(40):
        nested = '{"next":' + nested + '}'
    with MiniJsonHttpPeer({"/server/info": HttpReply(body=nested.encode())}) as peer:
        with pytest.raises(PassivePolicyError, match="nesting"):
            ReadOnlyHttpTransport(peer.base_url, "moonraker").get("/server/info")


def test_json_nesting_is_rejected_before_recursive_decoder_failure():
    nested = '"leaf"'
    for _ in range(2000):
        nested = '{"next":' + nested + '}'
    with MiniJsonHttpPeer({"/server/info": HttpReply(body=nested.encode())}) as peer:
        with pytest.raises(PassivePolicyError, match="nesting"):
            ReadOnlyHttpTransport(peer.base_url, "moonraker").get("/server/info")


def test_http_passive_wire_timeout_is_bounded():
    with MiniJsonHttpPeer({"/server/info": HttpReply(delay_seconds=0.2)}) as peer:
        with pytest.raises((TimeoutError, socket.timeout)):
            ReadOnlyHttpTransport(peer.base_url, "moonraker", timeout=0.03).get("/server/info")


def test_pinned_http_transport_connects_exact_validated_address_without_dns():
    with MiniJsonHttpPeer({"/server/info": {"result": {"moonraker_version": "ODIN-CERT-1"}}}) as peer:
        port = int(peer.base_url.rsplit(":", 1)[1])
        transport = PinnedReadOnlyHttpTransport(
            host="printer.cert.test", address="127.0.0.1", port=port, protocol="moonraker"
        )
        assert transport.get("/server/info")["result"]["moonraker_version"] == "ODIN-CERT-1"
    assert peer.requests == [("GET", "/server/info")]
