"""Correlate an H3C project_file reply on the existing dispatch connection."""
from contextlib import contextmanager
import json
import re
from queue import Empty, SimpleQueue
from threading import Event
from time import monotonic


class ProjectRejected(RuntimeError):
    """A confirmed failure before publish or an explicit rejection from the printer."""


class ProjectNotSent(ProjectRejected):
    """The local transport could not submit the command."""


class ProjectReplies:
    """Permanent callbacks for a dispatch client, installed before its network loop starts."""

    def __init__(self, client, lock):
        self._lock = lock
        self._active = None
        normal_message = client.on_message
        normal_subscribe = client.on_subscribe
        normal_disconnect = client.on_disconnect

        def on_message(client, userdata, message):
            with lock:
                if self._active is not None:
                    self._active[0](client, userdata, message)
            if normal_message is not None:
                normal_message(client, userdata, message)

        def on_subscribe(client, userdata, mid, codes, *properties):
            with lock:
                if self._active is not None:
                    self._active[1].put((mid, codes))
            if normal_subscribe is not None:
                normal_subscribe(client, userdata, mid, codes, *properties)

        def on_disconnect(*args):
            with lock:
                if self._active is not None:
                    self._active[1].put((None, None))
                self._active = None
            if normal_disconnect is not None:
                normal_disconnect(*args)

        client.on_message = on_message
        client.on_subscribe = on_subscribe
        client.on_disconnect = on_disconnect

    @contextmanager
    def listening(self, observe, grants):
        with self._lock:
            if self._active is not None:
                raise ProjectNotSent('An H3C start reply is already pending.')
            self._active = (observe, grants)
        try:
            yield
        finally:
            with self._lock:
                if self._active is not None and self._active[0] is observe:
                    self._active = None

    def active(self, observe):
        with self._lock:
            return self._active is not None and self._active[0] is observe


def publish_project(client, topic, publish, payload, *, timeout=5):
    command = payload['print']
    filename = command['url'].rsplit('/', 1)[-1]
    received = Event()
    submitted = Event()
    accepted = False
    detail = ''

    def observe(_client, _userdata, message):
        nonlocal accepted, detail
        if not submitted.is_set() or message.retain or message.topic != topic or len(message.payload) > 65536 or received.is_set():
            return
        try:
            reply = json.loads(message.payload)['print']
            if (reply.get('command') != 'project_file' or str(reply.get('sequence_id')) != str(command['sequence_id'])
                    or filename not in (reply.get('file'), str(reply.get('url', '')).rsplit('/', 1)[-1])):
                return
            result = str(reply.get('result', '')).upper()
            if result not in ('SUCCESS', 'FAIL', 'FAILED', 'FAILURE', 'ERROR'):
                return
            code = reply.get('err_code')
            accepted = result == 'SUCCESS' and code in (None, 0, '0')
            detail = f'H3C start reply: {result}'
            if type(code) is int or (isinstance(code, str) and re.fullmatch(r'[0-9A-Fa-fxX-]{1,16}', code)):
                detail += f' (code {code})'
            reason = reply.get('reason')
            # Keep recognizable printer diagnostics without recording free-form
            # text, which can contain private paths or connection information.
            if isinstance(reason, str) and reason.upper() in {
                    'FILE_NOT_FOUND', 'FILE_NOT_EXIST', 'FILE_MISSING', 'INVALID_FILE', 'BUSY', 'PRINTER_BUSY'}:
                detail += f' — {reason.upper()}'
            received.set()
        except (TypeError, ValueError, KeyError, AttributeError):
            return

    grants = SimpleQueue()
    with client.project_replies.listening(observe, grants):
        try:
            rc, mid = client.subscribe(topic)
        except Exception as exc:
            raise ProjectNotSent(f"Could not subscribe to the H3C reply ({type(exc).__name__}).") from None
        if rc != 0:
            raise ProjectNotSent(f"Could not subscribe to the H3C reply (MQTT code {rc}). No start command was sent.")
        deadline = monotonic() + timeout
        while True:
            try:
                ack_mid, codes = grants.get(timeout=max(0, deadline - monotonic()))
            except Empty:
                raise ProjectNotSent("H3C reply subscription was not confirmed. No start command was sent.") from None
            if ack_mid is None:
                raise ProjectNotSent("MQTT disconnected before the H3C start command.")
            if ack_mid == mid:
                code = getattr(codes[0], 'value', codes[0]) if codes and len(codes) == 1 else None
                if isinstance(code, int) and 128 <= code <= 255:
                    raise ProjectRejected(f"H3C reply subscription was refused (code {code}). No start command was sent.")
                if code not in (0, 1, 2):
                    raise ProjectNotSent("H3C reply subscription grant was invalid. No start command was sent.")
                break
        if not client.project_replies.active(observe):
            raise ProjectNotSent("MQTT disconnected before the H3C start command.")
        submitted.set()
        if not publish(payload):
            return False
        if not received.wait(timeout):
            return False  # Unknown outcome; dispatch keeps its reconciliation fence.
        if not accepted:
            raise ProjectRejected(detail)
        return True
