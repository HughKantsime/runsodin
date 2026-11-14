"""ITAR mode, go2rtc in its own container: a camera stream the running go2rtc still holds
from before is removed at every start of the API container.

Recheck 2026-11-13, finding 5 (still open): the preparation replaced the configuration
FILE, but in the enterprise layout go2rtc is another container that ODIN neither starts nor
stops. It keeps the streams it loaded in memory, so a public camera source saved before
ITAR mode was turned on stayed reachable through go2rtc after an API restart whose
preparation failed.

Now, under ITAR mode with ODIN_GO2RTC_API_URL set, the preparation asks the running go2rtc
for its streams and deletes every one that is not exactly in the configuration it has just
checked; if that cannot be done the status is 11 and the entrypoint refuses to start.

go2rtc is a stub here (httpx.MockTransport with go2rtc's stream API: GET and DELETE
/api/streams, HTTP basic credentials). No go2rtc binary or container runs.
"""
import base64

import httpx
import pytest

from modules.printers import camera_relay, go2rtc_access, go2rtc_purge
from modules.printers.go2rtc_purge import Go2rtcPurgeFailed, remove_streams_not_in
from scripts import prepare_go2rtc_config as prepare

GO2RTC = "http://10.9.0.8:1984"
PUBLIC_SOURCE = "rtsp://8.8.8.8:554/live"
LOCAL_SOURCE = "rtsp://192.168.1.40:554/s"


class Go2rtc:
    """The running go2rtc: its in-memory streams, and the credentials it was started with."""

    def __init__(self, streams, credentials):
        self.streams, self.credentials = dict(streams), credentials
        self.requests, self.delete_status, self.fail_with, self.forgets, self.list_status = [], 200, None, True, 200

    def __call__(self, request):
        self.requests.append((request.method, str(request.url)))
        assert request.url.host == "10.9.0.8" and request.url.port == 1984
        if self.fail_with is not None:
            raise self.fail_with
        user, password = self.credentials
        expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401)
        if request.url.path != "/api/streams":
            return httpx.Response(404)
        if request.method == "GET" and self.list_status != 200:
            return httpx.Response(self.list_status)
        if request.method == "GET":
            return httpx.Response(200, json={name: {"producers": [{"url": url} for url in urls], "consumers": []}
                                             for name, urls in self.streams.items()})
        if request.method == "DELETE":
            if self.delete_status == 200 and self.forgets:
                self.streams.pop(request.url.params["src"], None)
            return httpx.Response(self.delete_status)
        return httpx.Response(405)


@pytest.fixture
def enterprise(tmp_path, monkeypatch):
    """ITAR mode on, go2rtc at its own address, running with the PREVIOUS boot's credentials
    and holding one local and one public stream."""
    monkeypatch.setenv("ODIN_CAMERA_RELAY_SECRET_FILE", str(tmp_path / "relay-secret"))
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", GO2RTC)
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.delenv("ODIN_CAMERA_RELAY_BASE_URL", raising=False)
    camera_relay.rotate_relay_secret()
    running_with = go2rtc_access.go2rtc_api_credentials()
    camera_relay.rotate_relay_secret()          # this boot: go2rtc still has the old ones
    stub = Go2rtc({"printer_4": [LOCAL_SOURCE], "printer_7": [PUBLIC_SOURCE]}, running_with)
    clients = []
    real = httpx.Client

    def client(**kwargs):
        clients.append(kwargs)
        return real(**{**kwargs, "transport": httpx.MockTransport(stub)})
    monkeypatch.setattr(go2rtc_purge.httpx, "Client", client)
    stub.clients = clients
    return stub


def test_a_stream_that_is_not_in_the_checked_list_is_removed(enterprise):
    assert remove_streams_not_in({"printer_4": LOCAL_SOURCE}) == ["printer_7"]
    assert enterprise.streams == {"printer_4": [LOCAL_SOURCE]}
    assert enterprise.clients[0]["trust_env"] is False and enterprise.clients[0]["follow_redirects"] is False


def test_a_stream_with_a_checked_name_but_another_source_is_removed(enterprise):
    enterprise.streams = {"printer_4": [PUBLIC_SOURCE], "printer_5": [LOCAL_SOURCE, PUBLIC_SOURCE]}
    removed = remove_streams_not_in({"printer_4": LOCAL_SOURCE, "printer_5": [LOCAL_SOURCE]})
    assert sorted(removed) == ["printer_4", "printer_5"] and enterprise.streams == {}


def test_an_empty_checked_list_removes_everything(enterprise):
    assert sorted(remove_streams_not_in({})) == ["printer_4", "printer_7"] and enterprise.streams == {}


def test_nothing_is_removed_when_go2rtc_already_matches(enterprise):
    enterprise.streams = {"printer_4": [LOCAL_SOURCE]}
    assert remove_streams_not_in({"printer_4": LOCAL_SOURCE, "printer_9": LOCAL_SOURCE}) == []
    assert [method for method, _ in enterprise.requests if method == "DELETE"] == []


@pytest.mark.parametrize("break_it", ["delete_500", "timeout", "wrong_credentials", "not_forgotten", "list_500"])
def test_when_the_result_is_unknown_it_is_a_failure(enterprise, break_it):
    if break_it == "delete_500":
        enterprise.delete_status = 500
    elif break_it == "timeout":
        enterprise.fail_with = httpx.ReadTimeout("slow")
    elif break_it == "wrong_credentials":
        enterprise.credentials = ("odin", "someone-else")
    elif break_it == "not_forgotten":
        enterprise.forgets = False
    else:
        enterprise.list_status = 500
    with pytest.raises(Go2rtcPurgeFailed):
        remove_streams_not_in({"printer_4": LOCAL_SOURCE})


def test_a_go2rtc_address_outside_the_local_network_is_a_failure_and_is_not_contacted(enterprise, monkeypatch):
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://8.8.4.4:1984")
    with pytest.raises(Go2rtcPurgeFailed):
        remove_streams_not_in({})
    assert enterprise.requests == []


def test_a_go2rtc_that_is_not_running_holds_no_stream(enterprise):
    enterprise.fail_with = httpx.ConnectError("connection refused")
    assert remove_streams_not_in({}) == []


def test_go2rtc_beside_the_api_is_started_afresh_and_is_not_asked(enterprise, monkeypatch):
    monkeypatch.delenv("ODIN_GO2RTC_API_URL")
    assert remove_streams_not_in({}) == [] and enterprise.requests == [] and enterprise.clients == []


# ---- the preparation at container start ----

@pytest.fixture
def start(enterprise, tmp_path, monkeypatch):
    path = tmp_path / "go2rtc.yaml"
    path.write_text(f"streams:\n  printer_7: {PUBLIC_SOURCE}\n")
    monkeypatch.setenv("GO2RTC_CONFIG", str(path))
    monkeypatch.setattr(prepare, "LAUNCHED_CONFIG_PATH", str(path))
    import core.db
    from modules.printers import route_utils
    # The fixture already rotated the secret for "this boot"; go2rtc runs with the previous one.
    monkeypatch.setattr(camera_relay, "rotate_relay_secret", lambda: None)

    class Session:
        def close(self):
            pass
    monkeypatch.setattr(core.db, "SessionLocal", Session)
    monkeypatch.setattr(route_utils, "write_go2rtc_config_before_start", lambda config: None)
    state = {"fails": False}

    def build(db):
        if state["fails"]:
            raise OSError(28, "No space left on device")
        return {"streams": {"printer_4": LOCAL_SOURCE}}
    monkeypatch.setattr(route_utils, "_build_go2rtc_config", build)
    return enterprise, state, path


def test_every_start_removes_the_old_stream_when_the_preparation_succeeds(start):
    go2rtc, _state, _path = start
    assert prepare.main() == 0
    assert go2rtc.streams == {"printer_4": [LOCAL_SOURCE]}


def test_a_failed_preparation_empties_the_file_and_the_running_go2rtc(start):
    go2rtc, state, path = start
    state["fails"] = True
    assert prepare.main() == prepare.SAFE_TO_START_WITHOUT_CAMERAS
    assert go2rtc.streams == {} and "8.8.8.8" not in path.read_text()


@pytest.mark.parametrize("preparation_fails", [True, False])
def test_the_api_does_not_start_when_the_running_go2rtc_cannot_be_cleaned(start, preparation_fails, capsys):
    go2rtc, state, _path = start
    state["fails"] = preparation_fails
    go2rtc.delete_status = 500
    assert prepare.main() == prepare.STALE_CONFIGURATION_LEFT == 11
    assert PUBLIC_SOURCE in go2rtc.streams["printer_7"]
    assert "could not be checked for old camera streams" in capsys.readouterr().err
    from tests.test_contracts.test_itar_go2rtc_stale_config import _entrypoint
    code, output = _entrypoint("1", 11)
    assert code == 1 and "STARTED" not in output


def test_with_itar_off_the_running_go2rtc_is_not_touched(start, monkeypatch):
    go2rtc, state, _path = start
    monkeypatch.delenv("ODIN_ITAR_MODE")
    assert prepare.main() == 0
    state["fails"] = True
    assert prepare.main() == 1
    assert go2rtc.requests == [] and set(go2rtc.streams) == {"printer_4", "printer_7"}
