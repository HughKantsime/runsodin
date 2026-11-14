"""ITAR mode, go2rtc in its own container: go2rtc is restarted with the checked camera
configuration, and shown to have loaded it, before ODIN asks it for anything.

History (reviews of 2026-11-13):
  finding 5      a failed preparation left the old file for go2rtc.
  recheck 1 (B)  in the enterprise layout go2rtc is another container and keeps old streams
                 in memory whatever happens to the file.
  recheck 2      removing streams one by one does not stop their producers in go2rtc
                 v1.9.4; producer URLs are not the configured sources; any connection
                 error was read as "not running"; a go2rtc that came up late with the old
                 file was missed.
Round 3: a restart boundary (modules/printers/go2rtc_gate.py). go2rtc is told to restart
(POST /api/restart; v1.9.4 replaces its own process), must answer again, and must list the
mark stream of the file ODIN has just written and no name ODIN did not write. Only a
refused connection means "not running"; then every later request to go2rtc goes through
the same restart-and-check first.

go2rtc is a stub here. It models what the pinned v1.9.4 source does: POST /api/restart
re-executes the process, which reads its configuration file again (the stub reads the
real file ODIN wrote); GET /api/streams lists the names it has in memory; requests from
another host need the credentials of the configuration it loaded. No go2rtc binary or
container runs, and the restart is not timed against a real process.
"""
import base64
import errno
import ssl

import httpx
import pytest
import yaml
from fastapi import HTTPException

from modules.printers import camera_relay, go2rtc_access, go2rtc_gate
from modules.printers.go2rtc_gate import Go2rtcGateFailed, Go2rtcNotRunning
from scripts import prepare_go2rtc_config as prepare

GO2RTC = "http://10.9.0.8:1984"
PUBLIC_SOURCE = "rtsp://8.8.8.8:554/live"
LOCAL_SOURCE = "rtsp://192.168.1.40:554/s"


def _refused():
    error = httpx.ConnectError("All connection attempts failed")
    error.__cause__ = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    return error


def _tls_failure():
    error = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    error.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed")
    return error


class Go2rtc:
    """A go2rtc process: the stream names it holds in memory, the credentials of the
    configuration it loaded, and the file it reads when it (re)starts."""

    def __init__(self, config_file, credentials, running=True, loaded=("printer_7",)):
        self.config_file, self.credentials, self.running = config_file, credentials, running
        self.loaded = list(loaded)          # what the process read when it last started
        self.requests = []
        self.restart_takes = 2              # polls during which the old process still answers
        self.down_for = 1                   # polls during which nothing listens
        self.ignores_restart = False
        self.fail_with = None               # an exception for every request
        self.status = {}                    # (method, path) -> status code to answer instead
        self.restarts = 0                   # restart requests it accepted
        self._pending = None

    def start(self, new_credentials=None):
        """(Re)start: read the configuration file, as go2rtc does at start."""
        self.running = True
        self.loaded = list((yaml.safe_load(self.config_file.read_text()) or {}).get("streams") or {})
        if new_credentials:
            self.credentials = new_credentials

    def _tick(self):
        if self._pending is None:
            return
        self._pending -= 1
        if self._pending == self.down_for:
            self.running = False
        elif self._pending <= 0:
            self._pending = None
            self.start(self.new_credentials)

    def request(self, method, url, **kwargs):
        url = httpx.URL(url)
        assert (url.host, url.port) == ("10.9.0.8", 1984), "the checked address"
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        self.requests.append((method, url.path))
        if self.fail_with is not None:
            raise self.fail_with
        if method == "GET":
            self._tick()
        if not self.running:
            raise _refused()
        user, password = kwargs["auth"]
        if (user, password) != tuple(self.credentials):
            return httpx.Response(401)
        if (method, url.path) in self.status:
            return httpx.Response(self.status[(method, url.path)])
        if (method, url.path) == ("POST", "/api/restart"):
            self.restarts += 1
            if not self.ignores_restart:
                self._pending = self.restart_takes + self.down_for + 1
            return httpx.Response(200)
        if (method, url.path) == ("GET", "/api/streams"):
            return httpx.Response(200, json={name: {"producers": [], "consumers": []} for name in self.loaded})
        return httpx.Response(404)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def enterprise(tmp_path, monkeypatch):
    """ITAR mode on; go2rtc is its own service, running with the previous boot's credentials
    and holding printer_7 (a public source) in memory from before ITAR mode was turned on."""
    monkeypatch.setenv("ODIN_CAMERA_RELAY_SECRET_FILE", str(tmp_path / "relay-secret"))
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", GO2RTC)
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.delenv("ODIN_CAMERA_RELAY_BASE_URL", raising=False)
    config_file = tmp_path / "go2rtc.yaml"
    config_file.write_text(f"streams:\n  printer_7: {PUBLIC_SOURCE}\n")
    monkeypatch.setenv("GO2RTC_CONFIG", str(config_file))
    monkeypatch.setattr(prepare, "LAUNCHED_CONFIG_PATH", str(config_file))
    camera_relay.rotate_relay_secret()
    previous = go2rtc_access.go2rtc_api_credentials()
    camera_relay.rotate_relay_secret()
    stub = Go2rtc(config_file, previous)
    stub.new_credentials = go2rtc_access.go2rtc_api_credentials()
    monkeypatch.setattr(go2rtc_gate.httpx, "post", lambda url, **kw: stub.request("POST", url, **kw))
    monkeypatch.setattr(go2rtc_gate.httpx, "get", lambda url, **kw: stub.request("GET", url, **kw))
    clock = Clock()
    monkeypatch.setattr(go2rtc_gate, "_sleep", clock.sleep)
    monkeypatch.setattr(go2rtc_gate, "_clock", lambda: clock.now)
    go2rtc_gate._last_failure["at"] = None
    stub.clock = clock
    return stub


def _checked_file(stub, names=("printer_4",)):
    """What the preparation does: write the checked file with a mark, and open the gate."""
    mark = go2rtc_gate.new_mark()
    streams = {name: LOCAL_SOURCE for name in names}
    stub.config_file.write_text(yaml.safe_dump({"streams": {**streams, mark: []}}))
    go2rtc_gate.open_gate(names, mark)
    return mark


def _state():
    return go2rtc_gate._read_gate()["state"]


# ---- step 2: restart, wait, read the names ----

def test_go2rtc_is_restarted_and_then_holds_only_what_odin_wrote(enterprise):
    mark = _checked_file(enterprise)
    go2rtc_gate.pass_gate_now()
    assert enterprise.requests[0] == ("POST", "/api/restart")
    assert sorted(enterprise.loaded) == sorted(["printer_4", mark]), "the old public stream is gone from memory"
    assert _state() == "passed"
    before = len(enterprise.requests)
    go2rtc_gate.pass_gate_now()
    go2rtc_gate.require_gate()
    assert len(enterprise.requests) == before, "a passed gate sends nothing"


def test_the_old_process_still_answering_is_not_mistaken_for_the_new_one(enterprise):
    """The old process holds printer_4 too (same name, an older public source). Only the
    mark shows that the process answering has loaded the file ODIN just wrote."""
    enterprise.loaded = ["printer_4"]
    enterprise.restart_takes = 6
    mark = _checked_file(enterprise)
    go2rtc_gate.pass_gate_now()
    listings = [request for request in enterprise.requests if request == ("GET", "/api/streams")]
    assert len(listings) >= 8 and mark in enterprise.loaded and _state() == "passed"


def test_a_go2rtc_that_does_not_restart_is_a_failure(enterprise):
    enterprise.ignores_restart = True
    _checked_file(enterprise)
    with pytest.raises(Go2rtcGateFailed, match="before the restart"):
        go2rtc_gate.pass_gate_now()
    assert _state() == "pending" and "printer_7" in enterprise.loaded


def test_a_name_odin_did_not_write_is_a_failure(enterprise):
    """go2rtc loads a file that has the mark and a stream ODIN never wrote (its own
    configuration was edited, or it merges a second file)."""
    mark = _checked_file(enterprise)
    data = yaml.safe_load(enterprise.config_file.read_text())
    data["streams"]["front_door"] = PUBLIC_SOURCE
    enterprise.config_file.write_text(yaml.safe_dump(data))
    with pytest.raises(Go2rtcGateFailed, match="ODIN did not write"):
        go2rtc_gate.pass_gate_now()
    assert _state() == "pending" and mark in enterprise.loaded


@pytest.mark.parametrize("failure", ["tls", "timeout", "unreachable", "credentials", "restart_500", "restart_400",
                                     "list_500", "never_back"])
def test_every_other_failure_refuses(enterprise, failure):
    _checked_file(enterprise)
    if failure == "tls":
        enterprise.fail_with = _tls_failure()
    elif failure == "timeout":
        enterprise.fail_with = httpx.ConnectTimeout("timed out")
    elif failure == "unreachable":
        error = httpx.ConnectError("Network is unreachable")
        error.__cause__ = OSError(errno.ENETUNREACH, "Network is unreachable")
        enterprise.fail_with = error
    elif failure == "credentials":
        enterprise.credentials = ("odin", "someone-else")
    elif failure == "restart_500":
        enterprise.status[("POST", "/api/restart")] = 500
    elif failure == "restart_400":
        enterprise.status[("POST", "/api/restart")] = 400
    elif failure == "list_500":
        enterprise.status[("GET", "/api/streams")] = 500
    else:
        enterprise.down_for = 10_000
    with pytest.raises(Go2rtcGateFailed):
        go2rtc_gate.pass_gate_now()
    assert _state() == "pending"


def test_only_a_refused_connection_means_not_running(enterprise):
    enterprise.running = False
    _checked_file(enterprise)
    with pytest.raises(Go2rtcNotRunning):
        go2rtc_gate.pass_gate_now()
    assert _state() == "pending" and enterprise.requests == [("POST", "/api/restart")]
    assert go2rtc_gate._refused(_refused()) is True
    assert go2rtc_gate._refused(_tls_failure()) is False
    assert go2rtc_gate._refused(httpx.ConnectTimeout("t")) is False
    assert go2rtc_gate._refused(httpx.ConnectError("no cause given")) is False


def test_a_connection_cut_by_the_restart_itself_is_not_a_failure(enterprise, monkeypatch):
    mark = _checked_file(enterprise)
    real = enterprise.request

    def request(method, url, **kwargs):
        response = real(method, url, **kwargs)
        if method == "POST" and response.status_code == 200:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return response
    monkeypatch.setattr(enterprise, "request", request)
    go2rtc_gate.pass_gate_now()
    assert mark in enterprise.loaded and _state() == "passed"


# ---- step 3: the gate in front of every request ----

def test_until_the_gate_has_passed_odin_asks_go2rtc_for_nothing(enterprise):
    """go2rtc was not running when the API started. It then comes up LATE with the old file
    in memory (it read it before ODIN replaced it). The first ODIN request restarts it."""
    enterprise.running = False
    mark = _checked_file(enterprise)
    for _ in range(3):
        with pytest.raises(HTTPException) as refused:
            go2rtc_access.go2rtc_target()
        assert refused.value.status_code == 409 and "has not yet been restarted" in refused.value.detail
    assert set(enterprise.requests) == {("POST", "/api/restart")}, "nothing but the restart attempt was sent"
    enterprise.running, enterprise.loaded = True, ["printer_7"]      # up late, old streams in memory
    target = go2rtc_access.go2rtc_target()
    assert target.url == GO2RTC and target.remote is True
    assert sorted(enterprise.loaded) == sorted(["printer_4", mark]) and _state() == "passed"
    sent = len(enterprise.requests)
    go2rtc_access.go2rtc_target()
    assert len(enterprise.requests) == sent, "one restart, once"


def test_a_failed_check_is_not_repeated_on_every_camera_request(enterprise):
    enterprise.ignores_restart = True
    _checked_file(enterprise)
    for _ in range(5):
        with pytest.raises(HTTPException):
            go2rtc_access.go2rtc_target()
    assert enterprise.restarts == 1
    enterprise.clock.now += go2rtc_gate.RETRY_AFTER_FAILURE_SECONDS + 1
    enterprise.ignores_restart = False
    assert go2rtc_access.go2rtc_target().remote is True
    assert enterprise.restarts == 2


def test_with_no_record_of_a_checked_configuration_nothing_is_sent(enterprise):
    with pytest.raises(HTTPException) as refused:
        go2rtc_access.go2rtc_target()
    assert refused.value.status_code == 409 and enterprise.requests == []


def test_the_reload_odin_does_later_goes_through_the_gate_too(enterprise):
    """route_utils._go2rtc_reload (the API's start-up sync and every later change) also sends
    POST /api/restart. With the gate failing, its own request is never sent: the one restart
    go2rtc received is the gate's."""
    from modules.printers import route_utils
    enterprise.ignores_restart = True
    _checked_file(enterprise)
    for _ in range(2):
        with pytest.raises(HTTPException):
            route_utils._go2rtc_reload()
    assert enterprise.restarts == 1


def test_go2rtc_beside_the_api_and_itar_off_have_no_gate(enterprise, monkeypatch):
    monkeypatch.delenv("ODIN_GO2RTC_API_URL")
    assert go2rtc_access.go2rtc_target().remote is False
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", GO2RTC)
    monkeypatch.delenv("ODIN_ITAR_MODE")
    assert go2rtc_access.go2rtc_target().remote is True
    assert enterprise.requests == [] and go2rtc_gate._read_gate() is None


# ---- the preparation at container start ----

@pytest.fixture
def start(enterprise, monkeypatch):
    import core.db
    from modules.printers import route_utils
    monkeypatch.setattr(camera_relay, "rotate_relay_secret", lambda: None)   # the fixture rotated for this boot

    class Session:
        def close(self):
            pass
    monkeypatch.setattr(core.db, "SessionLocal", Session)
    monkeypatch.setattr(route_utils, "GO2RTC_CONFIG", str(enterprise.config_file))
    state = {"fails": False}

    def build(db):
        if state["fails"]:
            raise OSError(28, "No space left on device")
        return {"api": {"listen": ":1984"}, "streams": {"printer_4": LOCAL_SOURCE}}
    monkeypatch.setattr(route_utils, "_build_go2rtc_config", build)
    return enterprise, state


def _file_streams(stub):
    return yaml.safe_load(stub.config_file.read_text())["streams"]


def test_every_start_restarts_go2rtc_with_the_checked_file(start):
    go2rtc, _state_ = start
    assert prepare.main() == 0
    streams = _file_streams(go2rtc)
    marks = [name for name in streams if name.startswith(go2rtc_gate.MARK_PREFIX)]
    assert len(marks) == 1 and streams[marks[0]] == [] and streams["printer_4"] == LOCAL_SOURCE
    assert sorted(go2rtc.loaded) == sorted(["printer_4", marks[0]]) and _state() == "passed"
    first = marks[0]
    assert prepare.main() == 0, "the next start does it again, with a new mark"
    assert first not in _file_streams(go2rtc) and go2rtc.restarts == 2


def test_a_failed_preparation_empties_the_file_and_restarts_go2rtc_with_it(start):
    go2rtc, state = start
    state["fails"] = True
    assert prepare.main() == prepare.SAFE_TO_START_WITHOUT_CAMERAS
    streams = _file_streams(go2rtc)
    assert list(streams.values()) == [[]] and "8.8.8.8" not in go2rtc.config_file.read_text()
    assert go2rtc.loaded == list(streams) and _state() == "passed"


@pytest.mark.parametrize("preparation_fails", [True, False])
def test_the_api_does_not_start_when_go2rtc_cannot_be_shown_to_run_the_checked_file(start, preparation_fails, capsys):
    go2rtc, state = start
    state["fails"] = preparation_fails
    go2rtc.fail_with = _tls_failure()
    assert prepare.main() == prepare.STALE_CONFIGURATION_LEFT == 11
    assert "could not be restarted with the checked camera configuration" in capsys.readouterr().err
    from tests.test_contracts.test_itar_go2rtc_stale_config import _entrypoint
    code, output = _entrypoint("1", 11)
    assert code == 1 and "STARTED" not in output


def test_the_api_starts_when_go2rtc_is_not_running_and_the_gate_stays_closed(start, capsys):
    go2rtc, _state_ = start
    go2rtc.running = False
    assert prepare.main() == 0
    assert _state() == "pending" and "not running yet" in capsys.readouterr().err
    with pytest.raises(HTTPException):
        go2rtc_access.go2rtc_target()


def test_a_go2rtc_address_outside_the_local_network_refuses_the_start(start, monkeypatch):
    go2rtc, _state_ = start
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://8.8.4.4:1984")
    assert prepare.main() == 11 and go2rtc.requests == []


def test_go2rtc_beside_the_api_gets_no_mark_and_no_gate(start, monkeypatch):
    go2rtc, _state_ = start
    monkeypatch.delenv("ODIN_GO2RTC_API_URL")
    assert prepare.main() == 0
    assert list(_file_streams(go2rtc)) == ["printer_4"]
    assert go2rtc.requests == [] and go2rtc_gate._read_gate() is None


def test_with_itar_off_go2rtc_is_not_restarted_and_the_file_has_no_mark(start, monkeypatch):
    go2rtc, state = start
    monkeypatch.delenv("ODIN_ITAR_MODE")
    assert prepare.main() == 0
    assert list(_file_streams(go2rtc)) == ["printer_4"]
    state["fails"] = True
    assert prepare.main() == 1
    assert go2rtc.requests == [] and go2rtc.loaded == ["printer_7"] and go2rtc_gate._read_gate() is None


def test_the_stream_by_stream_removal_is_gone():
    from pathlib import Path
    printers = Path(go2rtc_gate.__file__).parent
    assert not (printers / "go2rtc_purge.py").exists()
    source = (printers / "go2rtc_gate.py").read_text(encoding="utf-8")
    assert "producers" not in source.split('"""', 2)[2], "runtime producer URLs are not compared"
