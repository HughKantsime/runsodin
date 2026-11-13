"""TEST ONLY: a scripted Elegoo SDCP printer for adapter, readiness and dispatch replays.

Transcripts are built from Elegoo's public SDCP documentation, never from captures of a real
printer. HTTP uploads reuse tests.support.octoprint_http.Octo (routes keyed by (method, path)).
"""
import json

UPLOAD_OK = {"code": "000000", "messages": None, "data": None, "success": True}


class FakeWebSocket:
    """Stands in for the socket ElegooPrinter holds. ``script(frame)`` returns the frames to deliver
    back (synchronously, as the printer's answer), an empty list for silence, or an exception to raise."""

    def __init__(self, script):
        self.script = script
        self.sent = []
        self.deliver = None

    def send(self, text):
        frame = json.loads(text)
        self.sent.append(frame)
        outcome = self.script(frame)
        if isinstance(outcome, BaseException):
            raise outcome
        for reply in outcome or []:
            self.deliver(self, json.dumps(reply))

    def close(self):
        pass


def attach(printer, script=lambda frame: []):
    """Make ``printer`` look connected to a fake printer running ``script``."""
    ws = FakeWebSocket(script)
    ws.deliver = printer._on_message
    printer._ws = ws
    printer._connected = True
    return ws


def response(frame, ack=0, **override):
    data = frame["Data"]
    body = {"Id": frame["Id"],
            "Data": {"Cmd": data["Cmd"], "Data": {"Ack": ack}, "RequestID": data["RequestID"],
                     "MainboardID": data["MainboardID"], "TimeStamp": 1},
            "Topic": f"sdcp/response/{data['MainboardID']}"}
    body["Data"].update(override)
    return body


def answer(ack=0):
    return lambda frame: [response(frame, ack)]


def status_frame(mainboard="MB1", current=0, print_status=0, topic=None):
    return {"Status": {"CurrentStatus": [current], "PrintInfo": {"Status": print_status}},
            "MainboardID": mainboard, "TimeStamp": 1, "Topic": topic or f"sdcp/status/{mainboard}"}


class FakeConnection:
    """What websocket.create_connection returns in the readiness read."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = False

    def settimeout(self, value):
        pass

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self):
        if not self.frames:
            raise TimeoutError("no frame")
        item = self.frames.pop(0)
        return item if isinstance(item, str) else json.dumps(item)

    def close(self):
        self.closed = True
