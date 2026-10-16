"""Group A: device destination policy (printers, smart plugs, cameras).

Every bypass form from the final review is sent through the real routers
and the real plug HTTP helper against a real loopback listener, which must
never receive a connection. Private LAN destinations stay allowed.
"""
import socket
import threading

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    cert_app, cert_client, fernet_env, review_db,
)


@pytest.fixture
def listener():
    """Real TCP listeners on 127.0.0.1 and ::1 (same port when possible) counting connections."""
    hits = []
    socks = []
    v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    v4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    v4.bind(("127.0.0.1", 0))
    port = v4.getsockname()[1]
    socks.append(v4)
    try:
        v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        v6.bind(("::1", port))
        socks.append(v6)
    except OSError:
        pass
    stop = threading.Event()

    def serve(sock):
        sock.listen(8)
        sock.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except (socket.timeout, OSError):
                continue
            hits.append(conn.getsockname())
            conn.close()
    threads = [threading.Thread(target=serve, args=(s,), daemon=True) for s in socks]
    for t in threads:
        t.start()
    yield port, hits
    stop.set()
    for t in threads:
        t.join(1)
    for s in socks:
        s.close()


@pytest.fixture
def resolver(monkeypatch):
    """Map synthetic names to addresses for the policy's resolver (no real DNS)."""
    import core.webhook_utils as webhook_utils
    names = {}
    real = webhook_utils._real_getaddrinfo

    def fake(host, port, *args, **kwargs):
        if host in names:
            value = names[host]
            ips = value() if callable(value) else value
            return [((socket.AF_INET6 if ":" in ip else socket.AF_INET), socket.SOCK_STREAM, 6, "",
                     (ip, port or 0, 0, 0) if ":" in ip else (ip, port or 0)) for ip in ips]
        return real(host, port, *args, **kwargs)
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", fake)
    return names


def bypass_hosts(port):
    return [
        f"127.0.0.1:{port}", f"[::1]:{port}", "::1", f"[::ffff:127.0.0.1]:{port}", "::ffff:7f00:1",
        f"2130706433:{port}", f"0x7f000001:{port}", f"0x7f.1:{port}", f"127.1:{port}",
        f"localhost:{port}", f"rebind-loopback.test:{port}", f"[0:0:0:0:0:ffff:7f00:1]:{port}",
        "169.254.169.254", "[fe80::1]", "[fd00:ec2::254]", f"0.0.0.0:{port}", f"[64:ff9b::7f00:1]:{port}",
        "224.0.0.1",
    ]


def test_policy_blocks_every_bypass_form_and_allows_private_lan(resolver, listener):
    from core.webhook_utils import DeviceDestinationBlocked, device_destination_ips
    port, hits = listener
    resolver["rebind-loopback.test"] = ["127.0.0.1"]
    for host in bypass_hosts(port):
        with pytest.raises(DeviceDestinationBlocked):
            device_destination_ips(host)
    for host in ("192.168.1.50", "10.0.0.5:8080", "172.16.4.4", "[fd12::5]:80", "192.0.2.41"):
        assert device_destination_ips(host)
    assert hits == []


def test_printer_plug_and_camera_routes_reject_bypass_hosts(cert_app, review_db, resolver, listener):
    port, hits = listener
    resolver["rebind-loopback.test"] = ["127.0.0.1"]
    operator = cert_app.roles["operator"]
    for host in bypass_hosts(port):
        assert cert_app.patch("/printers/301", headers=operator, json={"api_host": host}).status_code == 400, host
        assert cert_app.put("/printers/301/plug", headers=operator,
                            json={"type": "tasmota", "host": host}).status_code == 400, host
        bracketed = host if not host.count(":") > 1 or host.startswith("[") else f"[{host}]"
        assert cert_app.put("/printers/301/plug", headers=operator,
                            json={"type": "homeassistant", "host": f"http://{bracketed}"}).status_code == 400, host
        assert cert_app.patch("/printers/301", headers=operator,
                              json={"camera_url": f"rtsp://{bracketed}/stream"}).status_code == 400, host
    review_db.expire_all()
    assert review_db.execute(text("SELECT plug_type,camera_url FROM printers WHERE id=301")).one() == (None, None)
    assert hits == []


def test_unresolvable_host_is_400_not_500(cert_app, resolver):
    def fail():
        raise socket.gaierror(socket.EAI_NONAME, "synthetic resolver failure")
    resolver["unresolvable.test"] = fail
    response = cert_app.put("/printers/301/plug", headers=cert_app.roles["operator"],
                            json={"type": "tasmota", "host": "unresolvable.test"})
    assert response.status_code == 400
    assert cert_app.patch("/printers/301", headers=cert_app.roles["operator"],
                          json={"api_host": "unresolvable.test"}).status_code == 400


def test_private_lan_plug_and_camera_hosts_are_allowed(cert_app, monkeypatch):
    import modules.printers.route_utils as route_utils
    monkeypatch.setattr(route_utils, "_go2rtc_restart_with_lock", lambda config, **kw: None)
    operator = cert_app.roles["operator"]
    assert cert_app.put("/printers/301/plug", headers=operator, json={"type": "tasmota", "host": "192.168.1.50"}).status_code == 200
    assert cert_app.put("/printers/301/plug", headers=operator,
                        json={"type": "homeassistant", "host": "http://10.0.0.5:8123"}).status_code == 200
    assert cert_app.patch("/printers/301", headers=operator, json={"camera_url": "rtsp://[fd12::5]:554/s"}).status_code == 200


@pytest.mark.parametrize("url", ["http://192.168.1.5:8123/api/../x", "http://192.168.1.5:8123?x=1",
                                 "http://user:pw@192.168.1.5:8123", "http://192.168.1.5:8123/#frag"])
def test_home_assistant_url_must_be_scheme_host_port_only(cert_app, url):
    assert cert_app.put("/printers/301/plug", headers=cert_app.roles["operator"],
                        json={"type": "homeassistant", "host": url}).status_code == 400


def test_plug_request_is_pinned_against_dns_rebinding(resolver, listener):
    """Validated public-looking LAN address first, loopback on the next lookup: the
    connect must use the validated address, never re-resolve to the listener."""
    from modules.printers import plug_http_protocol
    port, hits = listener
    answers = iter([["192.0.2.60"], ["127.0.0.1"], ["127.0.0.1"]])
    resolver["rebinding-plug.test"] = lambda: next(answers)
    status, _ = plug_http_protocol.http_request(f"http://rebinding-plug.test:{port}/cm?cmnd=Power", timeout=1)
    assert status == 0
    assert hits == []


def test_plug_request_rejects_destination_that_now_resolves_to_loopback(resolver, listener):
    from modules.printers import plug_http_protocol
    port, hits = listener
    resolver["now-loopback.test"] = ["127.0.0.1"]
    status, body = plug_http_protocol.http_request(f"http://now-loopback.test:{port}/cm?cmnd=Power", timeout=1)
    assert (status, body) == (0, "Destination blocked by local policy")
    assert hits == []


@pytest.mark.parametrize("password", ["se@cret", "se#cret", "se?cret", "se/cret"])
def test_legacy_camera_password_with_reserved_characters_is_fully_masked(cert_app, review_db, password):
    review_db.execute(text("UPDATE printers SET camera_url=:u WHERE id=301"),
                      {"u": f"rtsp://admin:{password}@192.0.2.9:554/live"})
    review_db.commit()
    body = cert_app.get("/printers/301", headers=cert_app.roles["viewer"])
    assert "cret" not in body.text
    assert body.json()["camera_url"] == "rtsp://***@192.0.2.9:554/live"


@pytest.mark.parametrize("url", ["rtsp://admin:se@cret@192.0.2.88/s", "rtsp://admin:se#cret@192.0.2.88/s",
                                 "rtsp://admin:se?cret@192.0.2.88/s", "rtsp://admin:se/cret@192.0.2.88/s",
                                 "rtsp://user@127.0.0.1:554#@192.0.2.88/s"])
def test_unencoded_reserved_characters_in_camera_userinfo_are_rejected(cert_app, review_db, url):
    response = cert_app.patch("/printers/301", headers=cert_app.roles["operator"], json={"camera_url": url})
    assert response.status_code == 400
    assert "percent-encode" in response.text or "blocked host" in response.text
    review_db.expire_all()
    assert review_db.execute(text("SELECT camera_url FROM printers WHERE id=301")).scalar_one() is None


def test_percent_encoded_camera_password_is_accepted_and_masked(cert_app, review_db, monkeypatch):
    import core.crypto as crypto
    import modules.printers.route_utils as route_utils
    monkeypatch.setattr(route_utils, "_go2rtc_restart_with_lock", lambda config, **kw: None)
    url = "rtsp://admin:se%40cr%2Fet@192.0.2.88:554/s"
    response = cert_app.patch("/printers/301", headers=cert_app.roles["operator"], json={"camera_url": url})
    assert response.status_code == 200, response.text
    assert response.json()["camera_url"] == "rtsp://***@192.0.2.88:554/s"
    review_db.expire_all()
    assert crypto.decrypt(review_db.execute(text("SELECT camera_url FROM printers WHERE id=301")).scalar_one()) == url


@pytest.mark.parametrize("url", ["rtsp://192.0.2.88:554/live@main", "rtsp://192.0.2.88/stream?user=a@b",
                                 "rtsp://192.0.2.88/x#@frag"])
def test_at_sign_in_camera_path_or_query_is_not_userinfo(cert_app, review_db, monkeypatch, url):
    import modules.printers.route_utils as route_utils
    monkeypatch.setattr(route_utils, "_go2rtc_restart_with_lock", lambda config, **kw: None)
    if "#" in url:
        # '#' is not valid in a stored camera URL fragment-free form; masking only.
        assert route_utils.sanitize_camera_url(url) == url
        return
    response = cert_app.patch("/printers/301", headers=cert_app.roles["operator"], json={"camera_url": url})
    assert response.status_code == 200, response.text
    assert response.json()["camera_url"] == url  # not rejected, not over-masked
    assert route_utils.sanitize_camera_url("rtsp://u:p@192.0.2.88/live@main") == "rtsp://***@192.0.2.88/live@main"

