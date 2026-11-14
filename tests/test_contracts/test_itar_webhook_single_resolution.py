"""Under ITAR mode a webhook is sent to the address that was checked: one name lookup, one
rule applied to that answer, and the connection pinned to it.

Review 2026-11-13 finding 2: safe_post checked the ITAR rule on one lookup and then looked
the name up AGAIN for the public-only SSRF rule and pinned that second answer. A name that
answered local first and public second passed both and the public address was pinned.
trusted_post checked and then made an unpinned request. A stable local webhook was refused
by the SSRF rule, so ITAR mode permitted no webhook at all.

The resolver and the HTTP client are fakes. Nothing opens a connection.
"""
import socket
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import HTTPException

from core import webhook_utils
from core.webhook_utils import WebhookSSRFError, safe_post, trusted_post

PRIVATE = "10.20.0.5"
PUBLIC = "8.8.8.8"


def _answer(*addresses):
    return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in addresses]


class Resolver:
    """The real resolver, replaced: answers[0] on the first lookup, answers[1] on the second,
    the last answer repeats. The DNS pin stays active in front of it, as in production."""

    def __init__(self, monkeypatch, *answers):
        self.answers, self.calls = answers, []
        monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", self)

    def __call__(self, host, *args, **kwargs):
        answer = self.answers[min(len(self.calls), len(self.answers) - 1)]
        self.calls.append(host)
        return _answer(*answer)


class Client:
    """Stands in for httpx.Client. post() asks the (pinned) resolver where the connection
    would go, exactly as httpx does when it connects, and records it."""
    made = []

    def __init__(self, **kwargs):
        self.kwargs, self.connected = kwargs, None
        Client.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        self.connected = [info[4][0] for info in socket.getaddrinfo(parts.hostname, port)]
        return SimpleNamespace(status_code=200)


@pytest.fixture
def client(monkeypatch):
    Client.made = []
    monkeypatch.setattr(httpx, "Client", Client)

    def unpinned_post(url, **kwargs):
        raise AssertionError("an unpinned httpx.post was made")
    monkeypatch.setattr(httpx, "post", unpinned_post)
    return Client


@pytest.fixture
def itar_on(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")


@pytest.fixture
def itar_off(monkeypatch):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)


def test_a_name_that_answers_local_then_public_is_sent_to_the_local_address(itar_on, client, monkeypatch):
    resolver = Resolver(monkeypatch, (PRIVATE,), (PUBLIC,))
    safe_post("https://hooks.plant.example/alert", json={})
    assert resolver.calls == ["hooks.plant.example"], "the name is looked up exactly once"
    assert [c.connected for c in client.made] == [[PRIVATE]]
    assert client.made[0].kwargs["trust_env"] is False


@pytest.mark.parametrize("url", ["http://hooks.plant.example/alert", f"http://{PRIVATE}:9000/alert",
                                 "http://[fd12:3456::9]/alert"])
def test_a_stable_local_webhook_is_allowed_under_itar(itar_on, client, monkeypatch, url):
    Resolver(monkeypatch, (PRIVATE,))
    assert safe_post(url, json={}).status_code == 200
    assert len(client.made) == 1


@pytest.mark.parametrize("answer", [(PUBLIC,), (PRIVATE, PUBLIC), ("127.0.0.1",), ("169.254.169.254",),
                                    ("::1",), ("0.0.0.0",)])
def test_public_mixed_loopback_and_link_local_answers_are_refused_under_itar(itar_on, client, monkeypatch, answer):
    resolver = Resolver(monkeypatch, answer)
    with pytest.raises(WebhookSSRFError):
        safe_post("https://hooks.plant.example/alert", json={})
    assert client.made == [] and len(resolver.calls) == 1


@pytest.mark.parametrize("url", [f"https://{PUBLIC}/hook", "http://127.0.0.1:9000/hook",
                                 "http://169.254.169.254/latest", "http://[::1]/hook"])
def test_literal_addresses_outside_the_local_network_are_refused_under_itar(itar_on, client, monkeypatch, url):
    Resolver(monkeypatch, (PRIVATE,))
    with pytest.raises(WebhookSSRFError):
        safe_post(url, json={})
    assert client.made == []


def test_a_webhook_to_odins_own_api_port_is_refused_under_itar(itar_on, client, monkeypatch):
    Resolver(monkeypatch, (PRIVATE,))
    monkeypatch.setattr(webhook_utils, "own_interface_addresses",
                        lambda: frozenset({webhook_utils.ipaddress.ip_address(PRIVATE)}))
    with pytest.raises(WebhookSSRFError):
        safe_post(f"http://{PRIVATE}:8000/api/anything", json={})
    with pytest.raises(WebhookSSRFError):
        safe_post("http://hooks.plant.example:8000/api/anything", json={})
    assert client.made == []


def test_with_itar_off_the_ssrf_rule_is_unchanged(itar_off, client, monkeypatch):
    resolver = Resolver(monkeypatch, (PUBLIC,))
    safe_post("https://hooks.example.com/alert", json={})
    assert client.made[0].connected == [PUBLIC] and len(resolver.calls) == 1
    for answer in ((PRIVATE,), (PRIVATE, PUBLIC), ("127.0.0.1",)):
        Resolver(monkeypatch, answer)
        with pytest.raises(WebhookSSRFError, match="private/reserved"):
            safe_post("https://hooks.example.com/alert", json={})
    with pytest.raises(WebhookSSRFError, match="private/reserved"):
        safe_post(f"http://{PRIVATE}/alert", json={})
    assert len(client.made) == 1


def test_trusted_post_under_itar_is_pinned_to_the_one_checked_answer(itar_on, client, monkeypatch):
    resolver = Resolver(monkeypatch, (PRIVATE,), (PUBLIC,))
    trusted_post("https://api.telegram.org/botX/sendMessage", json={})
    assert resolver.calls == ["api.telegram.org"]
    assert [c.connected for c in client.made] == [[PRIVATE]]
    assert client.made[0].kwargs["trust_env"] is False


@pytest.mark.parametrize("answer", [(PUBLIC,), (PRIVATE, PUBLIC)])
def test_trusted_post_under_itar_refuses_a_public_answer(itar_on, client, monkeypatch, answer):
    Resolver(monkeypatch, answer)
    with pytest.raises(WebhookSSRFError, match="ITAR"):
        trusted_post("https://api.telegram.org/botX/sendMessage", json={})
    assert client.made == []


def test_trusted_post_with_itar_off_is_unchanged(itar_off, monkeypatch):
    """Unpinned, proxy-aware httpx.post, and ODIN looks nothing up itself."""
    resolver = Resolver(monkeypatch, (PRIVATE,))
    sent = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append((url, kw)) or SimpleNamespace(status_code=200))
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("a pinned client was built"))
    trusted_post("https://api.telegram.org/botX/sendMessage", json={"a": 1})
    assert sent == [("https://api.telegram.org/botX/sendMessage", {"timeout": 10, "json": {"a": 1}})]
    assert resolver.calls == []


def test_saving_a_webhook_address_follows_the_mode(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    webhook_utils._validate_webhook_url(f"http://{PRIVATE}/hook")
    webhook_utils._validate_webhook_url("http://hooks.plant.example/hook")
    for url in (f"https://{PUBLIC}/hook", "http://127.0.0.1/hook", "http://169.254.169.254/x", "ftp://10.0.0.1/x"):
        with pytest.raises(HTTPException):
            webhook_utils._validate_webhook_url(url)
    monkeypatch.delenv("ODIN_ITAR_MODE")
    webhook_utils._validate_webhook_url(f"https://{PUBLIC}/hook")
    with pytest.raises(HTTPException):
        webhook_utils._validate_webhook_url(f"http://{PRIVATE}/hook")


def test_the_test_button_precheck_does_no_second_lookup_under_itar(itar_on, monkeypatch):
    resolver = Resolver(monkeypatch, (PRIVATE,), (PUBLIC,))
    assert webhook_utils.resolve_and_check_webhook_url("http://hooks.plant.example/x") == "http://hooks.plant.example/x"
    assert resolver.calls == []


def test_the_public_only_rule_other_callers_ask_for_does_not_change_with_the_mode(itar_on, monkeypatch):
    """core.outbound_http (roster providers) calls _resolve_and_pin without local_only."""
    Resolver(monkeypatch, (PRIVATE,))
    with pytest.raises(WebhookSSRFError, match="private/reserved"):
        webhook_utils._resolve_and_pin("https://roster.example.com/x")
    with pytest.raises(WebhookSSRFError, match="private/reserved"):
        webhook_utils._resolve_and_pin(f"https://{PRIVATE}/x")


# ---- item C: the ports of ODIN's configured services are not webhook ports ----
#
# Recheck 1: with ITAR mode permitting local webhooks, go2rtc on a custom port and the
# PostgreSQL server were valid webhook targets. Recheck 2: comparing hosts needed a second
# name lookup that could disagree with the pinned one, and missed effective ports. Round 3:
# the test is the port alone, on any host, with no lookup.

@pytest.fixture
def services(monkeypatch):
    """go2rtc on a custom port and PostgreSQL by name, as an enterprise install configures them."""
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://10.2.0.8:1999")
    monkeypatch.setenv("DATABASE_URL", "postgresql://odin@postgres:5432/odin")
    monkeypatch.setenv("DATABASE_MAINTENANCE_URL", "postgresql://odin_maintenance@10.2.0.9:6432/postgres")
    monkeypatch.setattr(webhook_utils, "own_interface_addresses", lambda: frozenset())
    lookups = []

    def resolver(host, *args, **kwargs):
        lookups.append(host)
        return _answer({"db": "10.1.1.1"}.get(host, "10.2.0.50"))
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", resolver)
    return lookups


@pytest.mark.parametrize("url", [
    "http://10.2.0.8:1999/api/restart",          # go2rtc's API on its configured port
    "http://10.2.0.7:5432/",                     # the port DATABASE_URL names
    "http://10.2.0.9:6432/",                     # the maintenance URL's port
    "http://tickets.plant.example:5432/hook",    # the same port on ANY host: no host is compared
    "http://[fd12:3456::9]:1999/hook",
])
def test_a_webhook_on_a_configured_service_port_is_refused_under_itar(itar_on, client, services, url):
    with pytest.raises(WebhookSSRFError):
        safe_post(url, json={})
    assert client.made == []


@pytest.mark.parametrize("url", ["http://10.2.0.8:8080/hook", "http://10.2.0.7:9000/hook",
                                 "http://tickets.plant.example/hook", "https://tickets.plant.example/hook"])
def test_other_local_webhooks_still_work_under_itar(itar_on, client, services, url):
    assert safe_post(url, json={}).status_code == 200


def test_the_exclusion_makes_no_lookup_of_its_own(itar_on, client, services, monkeypatch):
    """Recheck 2, finding 7: the database is `db:5432`; a webhook to http://db:5432/ was
    looked up once for the pin and again for the exclusion, and the two answers differed."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://odin@db:5432/odin")
    with pytest.raises(WebhookSSRFError):
        safe_post("http://db:5432/", json={})
    assert services == ["db"], "one lookup: the webhook's own"
    assert webhook_utils.configured_service_ports() == {1999, 5432, 6432} and services == ["db"]


@pytest.mark.parametrize("environment, ports", [
    ({"DATABASE_URL": "postgresql://u@10.1.1.1/d"}, {5432}),                           # no port: 5432
    ({"DATABASE_URL": "postgresql+psycopg://u@[::ffff:10.1.1.1]:6000/d"}, {6000}),
    ({"DATABASE_URL": "postgres://u@/d"}, {5432}),                                     # the socket's port number
    ({"DATABASE_URL": "sqlite:////data/odin.db"}, set()),                              # SQLite has no server
    ({"ODIN_GO2RTC_API_URL": "http://go2rtc"}, {80}),                                  # where an http URL goes
    ({"ODIN_GO2RTC_API_URL": "https://go2rtc.plant.example"}, {443}),
    ({"ODIN_GO2RTC_API_URL": "http://go2rtc:1984"}, {1984}),
    ({}, set()),                                                                       # go2rtc beside the API: fixed list
])
def test_the_ports_are_the_effective_ones(monkeypatch, environment, ports):
    for name in ("DATABASE_URL", "DATABASE_MAINTENANCE_URL", "ODIN_GO2RTC_API_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert webhook_utils.configured_service_ports() == ports


def test_a_go2rtc_url_without_a_port_refuses_its_real_port_not_1984(itar_on, client, services, monkeypatch):
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://10.2.0.8")
    with pytest.raises(WebhookSSRFError):
        safe_post("http://10.2.0.8/api/restart", json={})
    assert safe_post("http://10.2.0.60:1984/hook", json={}).status_code == 200


@pytest.mark.parametrize("own, webhook", [("::ffff:10.1.1.1", "http://10.1.1.1:8000/x"),
                                          ("10.1.1.1", "http://[::ffff:10.1.1.1]:8000/x"),
                                          ("10.1.1.1", "http://10.1.1.1:8555/x")])
def test_the_fixed_list_compares_one_spelling_of_an_address(itar_on, client, services, monkeypatch, own, webhook):
    """ODIN's own address written as IPv4-mapped IPv6 on one side and plain IPv4 on the other."""
    monkeypatch.setattr(webhook_utils, "own_interface_addresses",
                        lambda: frozenset({webhook_utils.ipaddress.ip_address(own)}))
    with pytest.raises(WebhookSSRFError):
        safe_post(webhook, json={})
    assert safe_post("http://10.1.1.2:8000/x", json={}).status_code == 200, "another host, same fixed port"


def test_with_itar_off_the_configured_services_change_nothing(itar_off, client, services, monkeypatch):
    """ITAR off: every private address was and is refused; a public one is not compared with
    the configured services (no new refusal)."""
    monkeypatch.setattr(webhook_utils, "configured_service_ports",
                        lambda: pytest.fail("the configured services were consulted with ITAR mode off"))
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, *a, **k: _answer(PUBLIC))
    assert safe_post(f"http://{PUBLIC}:1999/hook", json={}).status_code == 200
    assert safe_post("http://hooks.example.com:5432/hook", json={}).status_code == 200
    with pytest.raises(WebhookSSRFError, match="private/reserved"):
        safe_post("http://10.2.0.8:1999/api/restart", json={})


def test_the_per_host_lookup_is_gone():
    for name in ("configured_service_endpoints", "is_configured_service_endpoint"):
        assert not hasattr(webhook_utils, name)
