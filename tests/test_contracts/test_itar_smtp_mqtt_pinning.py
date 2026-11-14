"""Under ITAR mode mail and MQTT connect to the address that was checked.

Review 2026-11-13 finding 1: the mail and MQTT clients called enforce_host_destination(host)
and then gave the host NAME to smtplib / paho, which looked it up again to connect (paho
again on every reconnect). A name that answered local for the check and public for the
connection received credentials, notifications, reports and smart-plug commands.

The resolver answers local first and public on every later lookup. socket.create_connection
is replaced, so the address each library would really connect to is recorded and nothing is
opened. The real smtplib and the real paho client run on top of a fake socket.

Not verified here: a real mail server, a real broker, or a real TLS handshake (see the two
TLS tests: they check which server name the library hands to TLS, not a certificate).
"""
import ast
import socket
import ssl
from pathlib import Path

import paho.mqtt.client as mqtt
import pytest

from core import itar_connect, webhook_utils
from core.itar import ItarOutboundBlocked

BACKEND = Path(__file__).resolve().parents[2] / "backend"
PRIVATE = "10.20.0.7"
PUBLIC = "8.8.8.8"


def _answer(*addresses):
    return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in addresses]


class Resolver:
    def __init__(self, monkeypatch, *answers):
        self.answers, self.calls = answers, []
        monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", self)

    def __call__(self, host, *args, **kwargs):
        answer = self.answers[min(len(self.calls), len(self.answers) - 1)]
        self.calls.append(host)
        return _answer(*answer)


class FakeSocket:
    """Enough of a socket for smtplib to read a greeting and for paho to send CONNECT."""

    def __init__(self, replies=b""):
        self._replies = replies

    def makefile(self, *args, **kwargs):
        import io
        return io.BytesIO(self._replies)

    def sendall(self, data):
        return None

    def send(self, data):
        return len(data)

    def recv(self, size):
        raise BlockingIOError()

    def close(self):
        return None

    def __getattr__(self, name):   # setsockopt, setblocking, settimeout, fileno, shutdown ...
        return lambda *args, **kwargs: 0


@pytest.fixture
def connections(monkeypatch):
    """Every address handed to socket.create_connection. A name here is a failure: the
    library would look it up itself."""
    made = []

    def create_connection(address, *args, **kwargs):
        made.append(address)
        return FakeSocket(b"220 mail ready\r\n250 ok\r\n250 ok\r\n221 bye\r\n")
    monkeypatch.setattr(socket, "create_connection", create_connection)
    return made


@pytest.fixture
def itar_on(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")


@pytest.fixture
def itar_off(monkeypatch):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)


# ---- mail ----

def test_mail_connects_to_the_checked_address_when_the_name_changes_its_answer(itar_on, connections, monkeypatch):
    resolver = Resolver(monkeypatch, (PRIVATE,), (PUBLIC,))
    server = itar_connect.smtp_connection("mail.plant.example", 587, timeout=5)
    assert connections == [(PRIVATE, 587)]
    assert resolver.calls == ["mail.plant.example"], "one lookup"
    assert server._host == "mail.plant.example", "starttls() still names the configured host"


def test_starttls_sends_the_host_name_not_the_address(itar_on, connections, monkeypatch):
    """smtplib passes self._host to wrap_socket as server_hostname; the certificate is
    matched against that name when the caller's context verifies certificates."""
    Resolver(monkeypatch, (PRIVATE,))
    server = itar_connect.smtp_connection("mail.plant.example", 587, timeout=5)
    names = []

    class Context:
        def wrap_socket(self, sock, server_hostname=None):
            names.append(server_hostname)
            return sock
    monkeypatch.setattr(server, "ehlo_or_helo_if_needed", lambda: None)
    monkeypatch.setattr(server, "has_extn", lambda name: True)
    monkeypatch.setattr(server, "docmd", lambda *a: (220, b"go ahead"))
    server.starttls(context=Context())
    assert names == ["mail.plant.example"]


@pytest.mark.parametrize("answer", [(PUBLIC,), (PRIVATE, PUBLIC)])
def test_mail_to_a_public_or_mixed_answer_is_refused_before_any_socket(itar_on, connections, monkeypatch, answer):
    Resolver(monkeypatch, answer)
    with pytest.raises(ItarOutboundBlocked):
        itar_connect.smtp_connection("mail.example.com", 25)
    with pytest.raises(ItarOutboundBlocked):
        itar_connect.smtp_connection(PUBLIC, 25)
    assert connections == []


def test_mail_to_a_name_that_does_not_resolve_is_refused(itar_on, connections, monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "unknown")
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", fail)
    with pytest.raises(ItarOutboundBlocked):
        itar_connect.smtp_connection("mail.plant.example", 25)
    assert connections == []


def test_a_pinned_mail_client_refuses_another_destination(itar_on, connections, monkeypatch):
    Resolver(monkeypatch, (PRIVATE,))
    server = itar_connect.smtp_connection("mail.plant.example", 587)
    with pytest.raises(ItarOutboundBlocked):
        server.connect("elsewhere.example.com", 587)
    assert connections == [(PRIVATE, 587)]


def test_mail_with_itar_off_is_plain_smtplib(itar_off, connections, monkeypatch):
    resolver = Resolver(monkeypatch, (PUBLIC,))
    server = itar_connect.smtp_connection("mail.example.com", 587, timeout=5)
    assert type(server) is itar_connect.smtplib.SMTP
    assert connections == [("mail.example.com", 587)], "the library is given the name, as before"
    assert resolver.calls == [], "ODIN looks nothing up itself"


# ---- MQTT ----

def _client(host="broker.plant.example", port=1883, **kwargs):
    return itar_connect.mqtt_client(host, port, mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311, **kwargs)


def test_mqtt_connects_and_reconnects_to_the_checked_address(itar_on, connections, monkeypatch):
    resolver = Resolver(monkeypatch, (PRIVATE,), (PUBLIC,))
    client = _client()
    assert client.connect("broker.plant.example", 1883, keepalive=60) == mqtt.MQTT_ERR_SUCCESS
    client.reconnect()          # what paho's network loop calls after a dropped connection
    client.reconnect()
    assert connections == [(PRIVATE, 1883)] * 3
    assert resolver.calls == ["broker.plant.example"], "one lookup, none on reconnect"
    assert client._host == "broker.plant.example", "TLS server name and certificate match use the name"


def test_mqtt_tls_is_given_the_host_name(itar_on, connections, monkeypatch):
    Resolver(monkeypatch, (PRIVATE,))
    client = _client(port=8883)
    names = []

    class Wrapped(FakeSocket):
        def do_handshake(self):
            return None

        def getpeercert(self):
            return {}

    class Context:
        check_hostname = True   # the context matches the certificate to server_hostname itself

        def wrap_socket(self, sock, server_hostname=None, do_handshake_on_connect=True):
            names.append(server_hostname)
            return Wrapped()
    client.tls_set_context(Context())
    client.connect("broker.plant.example", 8883)
    assert names == ["broker.plant.example"] and connections == [(PRIVATE, 8883)]


@pytest.mark.parametrize("answer", [(PUBLIC,), (PRIVATE, PUBLIC)])
def test_mqtt_to_a_public_or_mixed_answer_is_refused_before_any_socket(itar_on, connections, monkeypatch, answer):
    Resolver(monkeypatch, answer)
    with pytest.raises(ItarOutboundBlocked):
        _client("broker.example.com")
    with pytest.raises(ItarOutboundBlocked):
        _client(PUBLIC)
    assert connections == []


def test_a_pinned_mqtt_client_refuses_another_destination_and_ignores_a_proxy(itar_on, connections, monkeypatch):
    Resolver(monkeypatch, (PRIVATE,))
    client = _client()
    client._proxy = {"proxy_type": 1, "proxy_addr": "203.0.113.9", "proxy_port": 1080}
    client.connect("broker.plant.example", 1883)
    assert connections == [(PRIVATE, 1883)]
    with pytest.raises(ItarOutboundBlocked):
        client.connect("elsewhere.example.com", 1883)
    with pytest.raises(ItarOutboundBlocked):
        client.connect("broker.plant.example", 8883)
    assert connections == [(PRIVATE, 1883)]


def test_mqtt_with_itar_off_is_the_plain_paho_client(itar_off, connections, monkeypatch):
    resolver = Resolver(monkeypatch, (PUBLIC,))
    client = _client("broker.example.com")
    assert type(client) is mqtt.Client
    client.connect("broker.example.com", 1883)
    assert connections == [("broker.example.com", 1883)] and resolver.calls == []


def test_the_paho_hook_the_pin_relies_on_exists():
    """paho-mqtt is pinned to 2.1.0. If an upgrade removes this method the pin would be
    silently bypassed; mqtt_client refuses in that case and this test says why."""
    assert callable(getattr(mqtt.Client, "_create_socket_connection", None))
    source = Path(mqtt.__file__).read_text(encoding="utf-8")
    assert "sock = self._create_socket_connection()" in source
    assert "server_hostname=self._host" in source


# ---- the callers ----

def test_the_owned_plug_command_goes_to_the_checked_address(itar_on, connections, monkeypatch):
    import time
    from core.mqtt_connection_snapshot import MqttConnectionSnapshot
    from core.mqtt_power_protocol import send_owned_mqtt_on
    resolver = Resolver(monkeypatch, (PRIVATE,), (PUBLIC,))
    monkeypatch.setattr(mqtt.Client, "loop_start", lambda self: mqtt.MQTT_ERR_NO_CONN)
    snapshot = MqttConnectionSnapshot("broker.plant.example", 1883, "", "", False)
    assert send_owned_mqtt_on(snapshot, "plugs/1/power", time.monotonic() + 5) == "refused"
    assert connections == [(PRIVATE, 1883)] and resolver.calls == ["broker.plant.example"]
    Resolver(monkeypatch, (PUBLIC,))
    assert send_owned_mqtt_on(snapshot, "plugs/1/power", time.monotonic() + 5) == "refused"
    assert connections == [(PRIVATE, 1883)], "a public broker gets no connection"


def test_the_republish_test_connection_goes_to_the_checked_address(itar_on, connections, monkeypatch):
    from modules.notifications import mqtt_republish
    resolver = Resolver(monkeypatch, (PRIVATE,), (PUBLIC,))
    monkeypatch.setattr(mqtt.Client, "loop_start", lambda self: mqtt.MQTT_ERR_SUCCESS)
    monkeypatch.setattr(mqtt_republish, "_CONNACK_WAIT_SECONDS", 0.01)
    result = mqtt_republish.test_connection("broker.plant.example", 1883)
    assert result["success"] is False          # the fake broker never answers
    assert connections == [(PRIVATE, 1883)] and resolver.calls == ["broker.plant.example"]
    Resolver(monkeypatch, (PUBLIC,))
    assert mqtt_republish.test_connection("broker.example.com", 1883)["success"] is False
    assert connections == [(PRIVATE, 1883)]


SMTP_CALLERS = ("modules/notifications/alert_dispatcher.py", "modules/notifications/channels.py",
                "modules/organizations/routes_auth.py", "modules/organizations/routes_users.py",
                "modules/reporting/report_runner.py")
MQTT_CALLERS = ("modules/notifications/mqtt_republish.py", "core/mqtt_power_protocol.py")


def _calls(name):
    tree = ast.parse((BACKEND / name).read_text(encoding="utf-8"), filename=name)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            found.append(ast.unparse(node.func))
    return found


@pytest.mark.parametrize("name", SMTP_CALLERS)
def test_no_mail_caller_builds_its_own_smtp_client(name):
    calls = _calls(name)
    assert "smtp_connection" in calls
    assert not [call for call in calls if call.endswith("SMTP") or call.endswith("SMTP_SSL")]


@pytest.mark.parametrize("name", MQTT_CALLERS)
def test_no_configured_broker_caller_builds_its_own_mqtt_client(name):
    calls = _calls(name)
    assert "mqtt_client" in calls
    assert not [call for call in calls if call.endswith(".Client")]


def test_a_mail_caller_sends_through_the_checked_address(itar_on, connections, monkeypatch):
    from modules.reporting import report_runner
    resolver = Resolver(monkeypatch, (PRIVATE,), (PUBLIC,))
    report_runner.send_report_email({"host": "mail.plant.example", "port": 25, "use_tls": False},
                                    "ops@plant.example", "Report", "<p>hi</p>")
    assert connections == [(PRIVATE, 25)] and resolver.calls == ["mail.plant.example"]
    Resolver(monkeypatch, (PUBLIC,))
    assert report_runner.send_report_email({"host": "mail.example.com", "port": 25, "use_tls": False},
                                           "ops@plant.example", "Report", "<p>hi</p>") is False
    assert connections == [(PRIVATE, 25)]
