"""Moonraker mDNS discovery: parser, device policy, configured marking, route RBAC and audit."""
import struct
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import cert_app, cert_client, fernet_env, review_db  # noqa: F401


def lab(value):
    raw = value.encode()
    return bytes([len(raw)]) + raw


def name(*labels):
    return b"".join(lab(label) for label in labels) + b"\x00"


def ptr(offset):
    return struct.pack("!H", 0xC000 | offset)


def rr(owner, rtype, rdata, ttl=120, cls=1):
    return owner + struct.pack("!HHIH", rtype, cls, ttl, len(rdata)) + rdata


def packet(answers, additionals=(), flags=0x8400, questions=b"", qd=0):
    return (struct.pack("!6H", 0, flags, qd, len(answers), 0, len(additionals))
            + questions + b"".join(answers) + b"".join(additionals))


SVC = name("_moonraker", "_tcp", "local")


def printer_packet(instance="Voron", target="voron", ip=(192, 168, 1, 50), port=7125, ttl=120):
    inst = name(instance, "_moonraker", "_tcp", "local")
    tgt = name(target, "local")
    return packet([rr(SVC, 12, inst, ttl=ttl)],
                  [rr(inst, 33, struct.pack("!HHH", 0, 0, port) + tgt, ttl=ttl), rr(tgt, 1, bytes(ip), ttl=ttl)])


def compressed_packet():
    """PTR rdata, SRV owner, SRV target and A owner all use compression pointers (one 2-hop chain)."""
    buf = bytearray(12)
    svc_off = len(buf)
    buf += SVC
    local_off = svc_off + len(lab("_moonraker")) + len(lab("_tcp"))
    ptr_rdata = lab("Voron") + ptr(svc_off)
    buf += struct.pack("!HHIH", 12, 1, 120, len(ptr_rdata))
    inst_off = len(buf)
    buf += ptr_rdata
    srv_rdata = struct.pack("!HHH", 0, 0, 7125) + lab("printer") + ptr(local_off)
    buf += ptr(inst_off)
    rdata_start = len(buf) + 10
    buf += struct.pack("!HHIH", 33, 1, 120, len(srv_rdata)) + srv_rdata
    buf += ptr(rdata_start + 6) + struct.pack("!HHIH", 1, 1, 120, 4) + bytes((192, 168, 1, 60))
    struct.pack_into("!6H", buf, 0, 0, 0x8400, 0, 1, 0, 2)
    return bytes(buf)


def chain_packet(hops):
    """A question whose name is `hops` backward pointers deep."""
    buf = bytearray(struct.pack("!6H", 0, 0x8400, 1, 0, 0, 0))
    first = len(buf)
    buf += lab("a") + b"\x00"
    last = first
    for _ in range(hops):
        here = len(buf)
        buf += ptr(last)
        last = here
    return bytes(buf + ptr(last) + struct.pack("!HH", 12, 1))


from modules.printers import mdns_discovery as mdns  # noqa: E402


def test_valid_packet_builds_candidate():
    candidates = mdns.build_candidates([("192.168.1.50", mdns.parse_packet(printer_packet()))])
    assert candidates == [{"name": "Voron", "host": "voron.local", "ip": "192.168.1.50",
                           "port": 7125, "self_announced": True}]


def test_compressed_packet_with_pointer_chain():
    candidates = mdns.build_candidates([("192.168.1.60", mdns.parse_packet(compressed_packet()))])
    assert [(c["name"], c["host"], c["ip"], c["port"]) for c in candidates] == [
        ("Voron", "printer.local", "192.168.1.60", 7125)]


def test_candidate_from_another_sender_is_not_self_announced():
    candidates = mdns.build_candidates([("192.168.1.99", mdns.parse_packet(printer_packet()))])
    assert len(candidates) == 1 and candidates[0]["self_announced"] is False


def test_goodbye_record_and_foreign_service_are_ignored():
    assert mdns.build_candidates([("192.168.1.50", mdns.parse_packet(printer_packet(ttl=0)))]) == []
    other = packet([rr(name("_http", "_tcp", "local"), 12, name("x", "_http", "_tcp", "local"))])
    assert mdns.build_candidates([("192.168.1.50", mdns.parse_packet(other))]) == []


def test_every_truncation_is_refused_cleanly():
    for data in (printer_packet(), compressed_packet()):
        for cut in range(len(data)):
            with pytest.raises(mdns.MdnsParseError):
                mdns.parse_packet(data[:cut])


def test_pointer_loops_and_forward_pointers_are_refused():
    own = struct.pack("!6H", 0, 0x8400, 1, 0, 0, 0) + ptr(12) + struct.pack("!HH", 12, 1)
    forward = struct.pack("!6H", 0, 0x8400, 1, 0, 0, 0) + ptr(40) + struct.pack("!HH", 12, 1)
    for data in (own, forward):
        with pytest.raises(mdns.MdnsParseError):
            mdns.parse_packet(data)


def test_pointer_chain_is_bounded():
    assert mdns.parse_packet(chain_packet(3)) == []
    # The name reader itself: a chain deeper than the limit is refused, a short one resolves.
    deep = chain_packet(mdns.MAX_POINTER_JUMPS + 4)
    name_at = len(deep) - 4 - 2          # the final pointer, just before the question's type and class
    with pytest.raises(mdns.MdnsParseError, match="too many compression pointers"):
        mdns._read_name(deep, name_at)
    shallow = chain_packet(3)
    assert list(mdns._read_name(shallow, len(shallow) - 6)[0]) == ["a"]


def test_oversized_and_overcounted_packets_are_refused():
    with pytest.raises(mdns.MdnsParseError, match="oversized"):
        mdns.parse_packet(b"\x00" * (mdns.MAX_PACKET + 1))
    with pytest.raises(mdns.MdnsParseError, match="too many records"):
        mdns.parse_packet(struct.pack("!6H", 0, 0x8400, 0, 65535, 0, 0))


def test_non_responses_reserved_labels_and_long_names_are_refused():
    with pytest.raises(mdns.MdnsParseError, match="not a response"):
        mdns.parse_packet(printer_packet().replace(struct.pack("!H", 0x8400), struct.pack("!H", 0), 1))
    reserved = struct.pack("!6H", 0, 0x8400, 1, 0, 0, 0) + b"\x80abc\x00" + struct.pack("!HH", 12, 1)
    with pytest.raises(mdns.MdnsParseError, match="reserved label"):
        mdns.parse_packet(reserved)
    long_name = b"".join(lab("a" * 63) for _ in range(5)) + b"\x00"
    with pytest.raises(mdns.MdnsParseError, match="name too long"):
        mdns.parse_packet(struct.pack("!6H", 0, 0x8400, 1, 0, 0, 0) + long_name + struct.pack("!HH", 12, 1))


def test_bad_record_shapes_are_refused():
    inst = name("Voron", "_moonraker", "_tcp", "local")
    with pytest.raises(mdns.MdnsParseError):
        mdns.parse_packet(packet([rr(inst, 1, b"\x01\x02\x03")]))          # A of 3 bytes
    with pytest.raises(mdns.MdnsParseError):
        mdns.parse_packet(packet([rr(inst, 33, b"\x00\x00\x00\x00\x1b")]))  # short SRV


def test_candidates_and_names_are_bounded_and_printable():
    answers, additionals = [], []
    for index in range(90):
        inst = name(f"p{index}\x07" + "x" * 55 if index == 0 else f"p{index}", "_moonraker", "_tcp", "local")
        tgt = name(f"h{index}", "local")
        answers.append(rr(SVC, 12, inst))
        additionals += [rr(inst, 33, struct.pack("!HHH", 0, 0, 7125) + tgt), rr(tgt, 1, bytes((192, 168, 1, index + 1)))]
    packets = [("192.168.1.1", mdns.parse_packet(packet(answers[i:i + 15], additionals[2 * i:2 * i + 30]))) for i in range(0, 90, 15)]
    candidates = mdns.build_candidates(packets)
    assert len(candidates) == 90 and mdns.MAX_CANDIDATES == 256   # every distinct endpoint reaches the caller's sort
    assert candidates[0]["name"].isprintable() and len(candidates[0]["name"]) <= mdns.MAX_DISPLAY


def test_scan_reports_unusable_socket_as_no_multicast():
    def broken(*args, **kwargs):
        raise OSError("no route")
    result = mdns.scan(socket_factory=broken)
    assert result.error == mdns.ERROR_NO_MULTICAST and result.packets == []


def test_scan_sends_one_query_and_ignores_oversize_wrong_port_and_garbage():
    class FakeSocket:
        sent = []

        def __init__(self, *a, **k):
            self.queue = [(printer_packet(), ("192.168.1.50", 5353)),
                          (printer_packet(), ("192.168.1.51", 9999)),          # not from 5353
                          (b"\x00" * (mdns.MAX_PACKET + 1), ("192.168.1.52", 5353)),
                          (b"junk", ("192.168.1.53", 5353))]

        def setsockopt(self, *a): pass
        def bind(self, address): pass
        def settimeout(self, value): pass
        def close(self): pass

        def sendto(self, data, address):
            FakeSocket.sent.append((data, address))

        def recvfrom(self, size):
            if not self.queue:
                raise TimeoutError()
            return self.queue.pop(0)
    clock = iter(range(0, 100))
    result = mdns.scan(1.0, socket_factory=FakeSocket, clock=lambda: next(clock) * 0.01)
    assert FakeSocket.sent == [(mdns.build_query(), ("224.0.0.251", 5353))]
    assert [sender for sender, _ in result.packets] == ["192.168.1.50"]
    assert result.malformed == 2 and result.error is None


# --- device destination policy -------------------------------------------------------

from modules.printers import moonraker_discovery as discovery  # noqa: E402


def candidate(ip, port=7125):
    return {"name": "x", "host": "x.local", "ip": ip, "port": port, "self_announced": True}


@pytest.mark.parametrize("itar", ["", "1"])
def test_policy_keeps_only_local_addresses(monkeypatch, itar):
    monkeypatch.setenv("ODIN_ITAR_MODE", itar)
    keep = ["192.168.1.50", "10.1.2.3", "172.16.5.5"]
    drop = ["8.8.8.8", "127.0.0.1", "169.254.169.254", "169.254.1.1", "224.0.0.251", "0.0.0.0",
            "255.255.255.255", "100.64.0.1", "192.0.2.1", "not-an-ip"]
    kept, refused = discovery.offered_candidates([candidate(ip) for ip in keep + drop])
    assert [c["ip"] for c in kept] == keep
    assert refused == len(drop)


def test_policy_refuses_odins_own_service_ports(monkeypatch):
    import ipaddress
    import core.webhook_utils as webhook_utils
    monkeypatch.setattr(webhook_utils, "own_interface_addresses", lambda: frozenset({ipaddress.ip_address("192.168.1.9")}))
    kept, _ = discovery.offered_candidates([candidate("192.168.1.9", 8000), candidate("192.168.1.9", 7125)])
    assert [(c["ip"], c["port"]) for c in kept] == [("192.168.1.9", 7125)]


def _device(monkeypatch, respond):
    """A real local TCP listener standing in for the device; the destination policy is stubbed."""
    import contextlib
    import socket
    import threading
    import core.webhook_utils as webhook_utils
    server = socket.socket(); server.bind(("127.0.0.1", 0)); server.listen(1)
    seen = {}

    def serve():
        conn, _ = server.accept()
        try:
            seen["request"] = conn.recv(4096)
            respond(conn)
        except OSError:
            pass
        finally:
            conn.close(); server.close()
    threading.Thread(target=serve, daemon=True).start()

    @contextlib.contextmanager
    def fake_pin(url):
        seen["pinned"] = url
        yield
    monkeypatch.setattr(webhook_utils, "pinned_device_destination", fake_pin)
    return server.getsockname()[1], seen


def _answer(body, status=b"200 OK", extra=b""):
    return b"HTTP/1.0 " + status + b"\r\nContent-Type: application/json\r\n" + extra + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body


GOOD = b'{"result": {"state": "ready\\u0007", "hostname": "voron"}}'


def test_probe_is_pinned_credential_free_and_bounded(monkeypatch):
    port, seen = _device(monkeypatch, lambda conn: conn.sendall(_answer(GOOD)))
    assert discovery.probe_klippy("127.0.0.1", port) == {"klippy_state": "ready", "hostname": "voron"}
    assert seen["pinned"] == f"http://127.0.0.1:{port}/printer/info"            # inside the destination policy
    request = seen["request"].decode()
    assert request.startswith("GET /printer/info HTTP/1.0\r\n") and "Accept-Encoding: identity" in request
    lowered = request.lower()
    assert not any(word in lowered for word in ("authorization", "cookie", "x-api-key", "token"))   # no credentials

    port, _ = _device(monkeypatch, lambda conn: conn.sendall(_answer(b"{}", status=b"401 Unauthorized")))
    assert discovery.probe_klippy("127.0.0.1", port) is None
    assert discovery.probe_klippy("127.0.0.1", 1) is None                      # nothing listening


def test_probe_drops_oversized_trickled_and_compressed_answers(monkeypatch):
    """Codex sign-off 11-12 (two passes): the body was buffered before the size check, nothing
    bounded the total time (headers included), and a compressed answer was expanded first."""
    import gzip
    import time

    def flood(conn):
        conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n")
        for _ in range(64):
            conn.sendall(b"x" * 8192)
    port, _ = _device(monkeypatch, flood)
    assert discovery.probe_klippy("127.0.0.1", port) is None

    def trickle_headers(conn):
        for _ in range(40):                      # one header byte every 0.2 s: each read succeeds
            conn.sendall(b"H"); time.sleep(0.2)
    port, _ = _device(monkeypatch, trickle_headers)
    began = time.monotonic()
    assert discovery.probe_klippy("127.0.0.1", port) is None
    assert time.monotonic() - began < discovery.PROBE_TIMEOUT_SECONDS + 1.0     # one total deadline

    packed = gzip.compress(b'{"result": {"state": "ready"}}' + b" " * (8 * 1024 * 1024))
    port, _ = _device(monkeypatch, lambda conn: conn.sendall(_answer(packed, extra=b"Content-Encoding: gzip\r\n")))
    assert discovery.probe_klippy("127.0.0.1", port) is None                    # never decompressed


def test_aliases_of_one_endpoint_are_one_candidate():
    """Codex sign-off 11-12: several service names for one address and port used several slots."""
    tgt = name("h1", "local")
    answers, additionals = [], [rr(tgt, 1, bytes((192, 168, 1, 9)))]
    for alias in ("alpha", "beta", "gamma"):
        inst = name(alias, "_moonraker", "_tcp", "local")
        answers.append(rr(SVC, 12, inst))
        additionals.append(rr(inst, 33, struct.pack("!HHH", 0, 0, 7125) + tgt))
    candidates = mdns.build_candidates([("192.168.1.9", mdns.parse_packet(packet(answers, additionals)))])
    assert [(c["ip"], c["port"]) for c in candidates] == [("192.168.1.9", 7125)]


# --- route ---------------------------------------------------------------------------

@pytest.fixture
def discovery_client(cert_client, review_db, monkeypatch):  # noqa: F811
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from core.rate_limit import limiter
    from modules.printers import routes_discovery
    app = cert_client.app
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(routes_discovery.router)
    limiter.reset()
    review_db.execute(text(
        "INSERT INTO printers(id,name,is_active,api_type,api_host,org_id,shared,slot_count,tags,timelapse_enabled) VALUES "
        "(401,'known a',1,'moonraker','192.168.50.10:7125',1,0,1,'[]',0),"
        "(402,'known b',1,'moonraker','Voron-B.local',NULL,0,1,'[]',0),"
        "(403,'other org',1,'moonraker','192.168.50.12',2,0,1,'[]',0)"))
    review_db.commit()
    probes = []
    monkeypatch.setattr(discovery, "probe_klippy", lambda ip, port: probes.append((ip, port)) or
                        ({"klippy_state": "ready", "hostname": "d"} if ip.endswith(".13") else None))
    cert_client.probes = probes
    yield cert_client
    limiter.reset()


def fake_scan(*entries, error=None):
    def run():
        packets = [(sender, mdns.parse_packet(printer_packet(instance, target, ip, port)))
                   for sender, instance, target, ip, port in entries]
        return mdns.ScanResult(packets=packets, error=error)
    return run


def entry(ip, instance="P", target="p", sender=None, port=7125):
    return (sender or ip, instance, target, tuple(int(part) for part in ip.split(".")), port)


def run_scan(monkeypatch, *entries, error=None):
    monkeypatch.setattr(mdns, "scan", fake_scan(*entries, error=error))


def test_route_requires_admin_with_printers_add(discovery_client, monkeypatch):
    run_scan(monkeypatch)
    client = discovery_client
    url = "/printers/discover/moonraker"
    assert client.post(url).status_code in (401, 403)
    assert client.post(url, headers=client.roles["viewer"]).status_code == 403
    assert client.post(url, headers=client.roles["operator"]).status_code == 403
    assert client.post(url, headers=client.roles["admin"]).status_code == 200
    # Codex sign-off 11-12: admin only whatever the permissions matrix grants: the route carries
    # its own admin-role dependency beside the action check.
    from modules.printers import routes_discovery
    import inspect
    assert 'require_role("admin"' in inspect.getsource(routes_discovery.discover_moonraker_printers)


def test_route_marks_configured_hosts_filters_policy_and_probes_only_self_announced(discovery_client, monkeypatch):
    run_scan(monkeypatch,
             entry("192.168.50.10", "Known A", "known-a"),
             entry("192.168.50.11", "Known B", "voron-b"),
             entry("192.168.50.12", "Other Org", "other"),
             entry("192.168.50.13", "Fresh", "fresh"),
             entry("192.168.50.14", "Relayed", "relayed", sender="192.168.50.99"),
             entry("8.8.8.8", "Public", "public"))
    response = discovery_client.post("/printers/discover/moonraker", headers=discovery_client.roles["admin"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reason"] is None
    by_ip = {p["ip"]: p for p in body["printers"]}
    assert set(by_ip) == {"192.168.50.1" + str(n) for n in range(5)}      # 8.8.8.8 never returned
    assert by_ip["192.168.50.10"]["already_configured"] is True           # matched IP, port ignored
    assert by_ip["192.168.50.11"]["already_configured"] is True           # matched voron-b.local ~ Voron-B.local
    assert by_ip["192.168.50.12"]["already_configured"] is False          # another organisation's printer
    assert by_ip["192.168.50.13"]["already_configured"] is False
    assert by_ip["192.168.50.13"]["address"] == "192.168.50.13:7125"
    assert by_ip["192.168.50.13"]["confirmed"] is True
    assert by_ip["192.168.50.13"]["klippy_state"] == "ready" and by_ip["192.168.50.13"]["hostname"] == "d"
    assert by_ip["192.168.50.10"]["confirmed"] is None                    # already added: not probed
    assert by_ip["192.168.50.14"]["confirmed"] is None                    # relayed: never probed
    assert sorted(ip for ip, _ in discovery_client.probes) == ["192.168.50.12", "192.168.50.13"]
    # Printers not added yet are listed first, so they survive the result cap on a large farm.
    assert [p["already_configured"] for p in body["printers"]] == sorted(p["already_configured"] for p in body["printers"])


def test_route_empty_with_plain_reasons(discovery_client, monkeypatch):
    url, headers = "/printers/discover/moonraker", discovery_client.roles["admin"]
    run_scan(monkeypatch, error=mdns.ERROR_NO_MULTICAST)
    response = discovery_client.post(url, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"printers": [], "reason": discovery.REASON_NO_MULTICAST}
    run_scan(monkeypatch)
    assert discovery_client.post(url, headers=headers).json()["reason"] == discovery.REASON_NONE_HEARD
    run_scan(monkeypatch, entry("8.8.8.8"))
    assert discovery_client.post(url, headers=headers).json() == {"printers": [], "reason": discovery.REASON_NOT_LOCAL}


def test_route_reports_a_scan_already_running(discovery_client, monkeypatch):
    run_scan(monkeypatch, entry("192.168.50.13"))
    assert discovery._scan_lock.acquire(blocking=False)
    try:
        response = discovery_client.post("/printers/discover/moonraker", headers=discovery_client.roles["admin"])
    finally:
        discovery._scan_lock.release()
    assert response.status_code == 200 and response.json() == {"printers": [], "reason": discovery.REASON_BUSY}


def test_route_is_audited_without_addresses(discovery_client, review_db, monkeypatch):
    from core.models import AuditLog
    run_scan(monkeypatch, entry("192.168.50.13"), entry("8.8.8.8"))
    assert discovery_client.post("/printers/discover/moonraker", headers=discovery_client.roles["admin"]).status_code == 200
    review_db.rollback()
    rows = review_db.query(AuditLog).filter(AuditLog.action == "printer.discovery").all()
    assert len(rows) == 1
    details = str(rows[0].details)
    assert "'offered': 1" in details or '"offered": 1' in details
    assert "192.168" not in details and "8.8.8.8" not in details


def test_route_is_rate_limited(discovery_client, monkeypatch):
    run_scan(monkeypatch)
    headers = discovery_client.roles["admin"]
    statuses = [discovery_client.post("/printers/discover/moonraker", headers=headers).status_code for _ in range(11)]
    assert statuses[:10] == [200] * 10 and statuses[10] == 429
