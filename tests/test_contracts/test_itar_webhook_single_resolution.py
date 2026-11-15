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

def _database(monkeypatch, url, maintenance=None):
    """The database the application is configured with: the settings object the engine is
    built from (core.config.settings), not the process environment."""
    from core import config
    monkeypatch.setattr(config.settings, "database_url", url)
    monkeypatch.setattr(config.settings, "database_maintenance_url", maintenance)


@pytest.fixture
def services(monkeypatch):
    """go2rtc on a custom port and PostgreSQL by name, as an enterprise install configures them."""
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://10.2.0.8:1999")
    _database(monkeypatch, "postgresql://odin@postgres:5432/odin",
              "postgresql://odin_maintenance@10.2.0.9:6432/postgres")
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
    _database(monkeypatch, "postgresql://odin@db:5432/odin")
    with pytest.raises(WebhookSSRFError):
        safe_post("http://db:5432/", json={})
    assert services == ["db"], "one lookup: the webhook's own"
    assert webhook_utils.configured_service_ports() == {1999, 5432} and services == ["db"]


@pytest.mark.parametrize("database, maintenance, go2rtc, ports", [
    ("postgresql://u@10.1.1.1/d", None, None, {5432}),                           # no port: 5432
    ("postgresql+psycopg://u@[::ffff:10.1.1.1]:6000/d", None, None, {6000}),
    ("postgres://u@/d", None, None, {5432}),                                     # the socket's port number
    ("postgresql://u@db:6432/d", "postgresql://m@db:7432/postgres", None, {6432, 7432}),
    ("sqlite:////data/odin.db", None, None, set()),                              # SQLite has no server
    ("sqlite:////data/odin.db", None, "http://go2rtc", {80}),                    # where an http URL goes
    ("sqlite:////data/odin.db", None, "https://go2rtc.plant.example", {443}),
    ("sqlite:////data/odin.db", None, "http://go2rtc:1984", {1984}),
])
def test_the_ports_are_the_effective_ones(monkeypatch, database, maintenance, go2rtc, ports):
    monkeypatch.delenv("ODIN_GO2RTC_API_URL", raising=False)
    if go2rtc:
        monkeypatch.setenv("ODIN_GO2RTC_API_URL", go2rtc)
    _database(monkeypatch, database, maintenance)
    assert webhook_utils.configured_service_ports() == ports


@pytest.mark.parametrize("how", ["lower_case_variable", "env_file", "upper_case_variable"])
def test_the_database_port_comes_from_the_settings_the_engine_is_built_from(itar_on, client, monkeypatch, tmp_path, how):
    """Recheck 3, finding 5: the application reads its database URL through its settings,
    which also take a .env file and variable names in any letter case. The exclusion read
    os.environ["DATABASE_URL"] only: with `database_url=postgresql://odin@10.9.0.7:6432/odin`
    the server used that database and a webhook to http://10.9.0.7:6432/ was accepted."""
    from core import config
    for name in ("DATABASE_URL", "database_url", "DATABASE_MAINTENANCE_URL", "database_maintenance_url",
                 "ODIN_GO2RTC_API_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    url, maintenance = "postgresql://odin@10.9.0.7:6432/odin", "postgresql://odin_maintenance@10.9.0.7:7432/postgres"
    if how == "lower_case_variable":
        monkeypatch.setenv("database_url", url)
        monkeypatch.setenv("database_maintenance_url", maintenance)
    elif how == "env_file":
        (tmp_path / ".env").write_text(f"DATABASE_URL={url}\nDatabase_Maintenance_Url={maintenance}\n")
    else:
        monkeypatch.setenv("DATABASE_URL", url)
        monkeypatch.setenv("DATABASE_MAINTENANCE_URL", maintenance)
    effective = config.Settings()                      # what the application would load
    assert effective.database_url == url, "the engine is built from this"
    monkeypatch.setattr(config, "settings", effective)
    monkeypatch.setattr(webhook_utils, "own_interface_addresses", lambda: frozenset())
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, *a, **k: _answer("10.9.0.50"))
    assert webhook_utils.configured_service_ports() == {6432, 7432}
    for port in (6432, 7432):
        with pytest.raises(WebhookSSRFError):
            safe_post(f"http://10.9.0.7:{port}/", json={})
    assert safe_post("http://10.9.0.7:8080/hook", json={}).status_code == 200


def test_the_exclusion_does_not_read_the_database_url_from_the_environment(monkeypatch):
    """A DATABASE_URL in os.environ that the settings object does not hold (set after the
    settings were loaded) is not what the engine uses, and is not what is excluded."""
    monkeypatch.delenv("ODIN_GO2RTC_API_URL", raising=False)
    _database(monkeypatch, "postgresql://odin@db:6432/odin")
    monkeypatch.setenv("DATABASE_URL", "postgresql://odin@elsewhere:9999/odin")
    assert webhook_utils.configured_service_ports() == {6432}
    source = webhook_utils.configured_service_ports.__code__
    assert "DATABASE_URL" not in source.co_consts and "DATABASE_MAINTENANCE_URL" not in source.co_consts


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
