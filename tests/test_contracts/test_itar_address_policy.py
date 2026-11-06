"""ITAR boot checks and request pins share an explicit local-address policy."""

import socket

import pytest

from core import itar, webhook_utils


@pytest.mark.parametrize("address", [
    "2001::808:808", "2001::f5ff:fffe",  # Teredo, including embedded private client
    "2002:0808:0808::", "2002:0a00:0001::",  # 6to4
    "64:ff9b::808:808", "64:ff9b::a00:1",  # NAT64 well-known prefix
    "64:ff9b:1::808:808", "64:ff9b:1::a00:1",  # NAT64 local-use prefix
    "::ffff:8.8.8.8", "2001:db8::1", "0.0.0.0", "::", "192.0.2.1",
])
@pytest.mark.parametrize("literal", [True, False])
def test_nonlocal_and_transition_ranges_fail_closed(monkeypatch, address, literal):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, port, **kw: [
        (family, socket.SOCK_STREAM, 6, "", (address, port)),
    ])
    host = address if literal else "transition.test"
    url_host = f"[{host}]" if ":" in host else host
    assert not itar.is_private_destination(host)
    assert not itar.check_url_allowed(f"http://{url_host}")[0]
    with pytest.raises(itar.ItarOutboundBlocked):
        with itar.pin_for_request(f"http://{url_host}"):
            pytest.fail("disallowed address was pinned")


@pytest.mark.parametrize("address", [
    "10.1.2.3", "172.16.0.1", "192.168.1.1", "127.0.0.1", "169.254.1.1",
    "fc00::1", "fd12::1", "::1", "fe80::1", "::ffff:10.1.2.3",
])
def test_local_ranges_remain_allowed(monkeypatch, address):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    assert itar.is_private_destination(address)
    host = f"[{address}]" if ":" in address else address
    with itar.pin_for_request(f"http://{host}"):
        assert webhook_utils._dns_pin_state.get()["ips"] == [address]
