"""Conservative raw-response checks for future powered-printer dispatch.

Callers must establish fresh transport receipt within their deadline. These
checks do not prove bed clearance, current authority or exclusion of another
printer controller. No cached or normalized dashboard state is accepted.
"""
import ipaddress
import json
import math
import re
import time

import requests
from requests.auth import HTTPDigestAuth

_MAX_STATUS_BYTES = 65536


class _BoundedDigestAuth(HTTPDigestAuth):
    """Bound the challenge body before requests performs its digest retry."""
    def __init__(self, username, password, deadline):
        super().__init__(username, password)
        self._deadline = deadline

    def handle_401(self, response, **kwargs):
        if 400 <= response.status_code < 500:
            try:
                body = bytearray()
                if time.monotonic() >= self._deadline:
                    raise ValueError('Digest challenge unavailable')
                for chunk in response.iter_content(chunk_size=4096):
                    body.extend(chunk)
                    if len(body) > _MAX_STATUS_BYTES or time.monotonic() >= self._deadline:
                        raise ValueError('Digest challenge unavailable')
                # requests.handle_401 consumes response.content internally.
                # Cache only the already bounded body, never the raw stream.
                response._content = bytes(body)
                response._content_consumed = True
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('Digest challenge unavailable')
                kwargs['timeout'] = remaining
            except Exception:
                response.close()
                raise
        return super().handle_401(response, **kwargs)


def fresh_http_status_is_idle(creds: dict, deadline: float) -> bool:
    """One fresh read, with a bounded admission deadline and no fallback.

    Requests timeouts limit inactivity, not total DNS/digest/read duration.
    Late responses always refuse admission; this is not a hard runtime bound.
    """
    try:
        if not isinstance(creds, dict) or isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            return False
        deadline = float(deadline)
        if not math.isfinite(deadline):
            return False
        protocol = creds.get('api_type')
        if protocol == 'elegoo':
            return fresh_sdcp_status_is_idle(creds, deadline)  # SDCP status arrives over a WebSocket, not HTTP
        endpoint = {'moonraker':'/printer/objects/query?webhooks&print_stats&virtual_sdcard',
                    'prusalink':'/api/v1/status',
                    'octoprint':'/api/printer'}.get(protocol)
        if endpoint is None:
            return False
        host, port = creds.get('ip'), creds.get('port', 80)
        if not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535:
            return False
        if ':' in host:
            host = '[' + str(ipaddress.IPv6Address(host)) + ']'
        elif re.fullmatch(r'[A-Za-z0-9_.-]+', host) is None:
            return False
        scheme = creds.get('scheme', 'http')
        if scheme not in ('http', 'https'):
            return False
        url = f'{scheme}://{host}:{port}{endpoint}'
        api_key = creds.get('api_key', '')
        if not isinstance(api_key, str) or any(ord(char) < 32 or ord(char) == 127 for char in api_key):
            return False
        headers = {'X-Api-Key':api_key} if api_key else {}
        auth = None
        if protocol == 'octoprint' and not api_key:
            return False  # an OctoPrint upload needs the key, so admission needs it too
        if protocol == 'prusalink' and not api_key:
            username, password = creds.get('username', 'maker'), creds.get('password', '')
            if not isinstance(username, str) or not isinstance(password, str):
                return False
            auth = _BoundedDigestAuth(username, password, deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        from core.itar import enforce_request_destination
        enforce_request_destination(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        from core.webhook_utils import pinned_device_destination
        # Device request: policy-validated and pinned (no DNS rebinding), no
        # redirects, no environment proxies.
        with requests.Session() as client, pinned_device_destination(url):
            client.trust_env = False
            client.proxies = {}
            client.max_redirects = 0
            with client.get(url, headers=headers, auth=auth, timeout=remaining,
                            allow_redirects=False, stream=True) as response:
                if response.status_code != 200 or time.monotonic() >= deadline:
                    return False
                body = bytearray()
                for chunk in response.iter_content(chunk_size=4096):
                    if time.monotonic() >= deadline:
                        return False
                    body.extend(chunk)
                    if len(body) > _MAX_STATUS_BYTES:
                        return False
                payload = json.loads(body)
                return time.monotonic() < deadline and raw_status_is_idle(protocol, payload)
    except Exception:
        # Do not expose configured credentials, network errors or device bodies.
        return False


def fresh_sdcp_status_is_idle(creds: dict, deadline: float) -> bool:
    """Elegoo SDCP: one fresh status read over the printer's WebSocket, bounded by ``deadline``.

    Sends Cmd 0 (status request, id used by this repo) and accepts only a status frame on
    ``sdcp/status/<MainboardID>`` whose own MainboardID matches the configured one. Idle only when
    the repo's parser says IDLE (CurrentStatus 0 and PrintInfo.Status 0). A finished-but-not-reset
    print, an unknown state, a mismatched board, silence or any error is not idle. The WebSocket
    connects to the policy-validated IP literal (device_connect_ip)."""
    conn = None
    try:
        host, port, mainboard = creds.get('ip'), creds.get('port', 3030), creds.get('api_key')
        if (not isinstance(host, str) or not host or type(port) is not int or not 1 <= port <= 65535
                or not isinstance(mainboard, str) or not mainboard
                or any(ord(char) < 32 or ord(char) == 127 for char in mainboard)):
            return False
        if ':' not in host and re.fullmatch(r'[A-Za-z0-9_.-]+', host) is None:
            return False
        import uuid
        import websocket
        from core.webhook_utils import device_connect_ip
        from modules.printers.adapters.elegoo import SDCPCommand, sdcp_request_frame
        from modules.printers.parsing.elegoo import parse_status
        address = device_connect_ip(host, port)
        shown = f'[{address}]' if ':' in address else address
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        conn = websocket.create_connection(f'ws://{shown}:{port}/websocket', timeout=remaining)  # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- by design: Elegoo SDCP is ws:// on the LAN
        conn.send(json.dumps(sdcp_request_frame(mainboard, SDCPCommand.STATUS_REQUEST, None, str(uuid.uuid4()))))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            conn.settimeout(remaining)
            message = conn.recv()
            if len(message) > _MAX_STATUS_BYTES:
                return False
            frame = json.loads(message)
            if not isinstance(frame, dict) or frame.get('Topic') != f'sdcp/status/{mainboard}':
                continue
            parsed = parse_status(frame)
            if not parsed:
                continue
            if parsed.get('mainboard_id') != mainboard:
                return False
            return time.monotonic() < deadline and parsed.get('internal_state') == 'IDLE'
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


BAMBU_TASK_CANCELED = 0x0300400C  # 50348044


def raw_status_is_idle(protocol: str, payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if protocol == 'bambu':
        # Automatic power preparation requires explicit no-error evidence.
        # Missing print_error is valid partial telemetry but not this gate.
        error = payload.get('print_error')
        state = payload.get('gcode_state')
        if type(error) is not int:
            return False
        # A stopped print leaves FAILED with Bambu's "task canceled"
        # acknowledgement, which clean_print_error does not reset on H2D.
        return (state in ('IDLE', 'FINISH', 'FAILED') and error == 0
                or state == 'FAILED' and error == BAMBU_TASK_CANCELED)
    if protocol == 'moonraker':
        result = payload.get('result')
        if not isinstance(result, dict):
            return False
        status = result.get('status')
        if not isinstance(status, dict):
            return False
        webhooks, stats, sd = (status.get(key) for key in ('webhooks', 'print_stats', 'virtual_sdcard'))
        return (isinstance(webhooks, dict) and isinstance(stats, dict) and isinstance(sd, dict)
                and webhooks.get('state') == 'ready'
                and stats.get('state') in ('standby', 'complete', 'cancelled')
                and sd.get('is_active') is False)
    if protocol == 'prusalink':
        printer = payload.get('printer')
        job = payload.get('job')
        return (isinstance(printer, dict) and printer.get('state') in ('IDLE', 'READY')
                and (job is None or isinstance(job, dict) and not job))
    if protocol == 'octoprint':
        # GET /api/printer state.flags: operational and ready with nothing else set. A 409
        # (not operational) never reaches here: only HTTP 200 is parsed.
        from modules.printers.parsing.octoprint import printer_state
        return printer_state(payload) == 'IDLE'
    return False
