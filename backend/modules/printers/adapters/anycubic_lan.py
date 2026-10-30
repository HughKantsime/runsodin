"""Stock Anycubic LAN monitor with one restricted, one-shot S1 start method.

Handshake/MQTT sequence adapted from this project's Phase A s1_capture.py.
Self-signed printer TLS requires verification disabled, as in the capture.
All network destinations are pinned to the policy-validated configured host.
"""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import ssl
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from modules.printers.parsing.anycubic_lan import parse_report, valid_report

log = logging.getLogger(__name__)
PREFIX = 'anycubic/anycubicCloud/v1'
KOBRA_S1_MODEL_ID = '20025'  # /info on the S1; 20029 is the S1 Max, which is a different printer.
ALLOWED_ACTIONS = frozenset({'query', 'getInfo'})
DEFAULTS = dict(internal_state='OFFLINE', bed_temp=0, bed_target=0, nozzle_temp=0,
                nozzle_target=0, box_temp=0, model_fan=0, filename='', progress_percent=0,
                current_layer=0, total_layers=0, current_ticks=0, time_remaining=0, boxes=[])


def decrypt_handshake(token, local_token, encrypted):
    iv = local_token.encode()[:16].ljust(16, b'\0')
    dec = Cipher(algorithms.AES(token[16:32].encode()), modes.CBC(iv)).decryptor()
    padded = dec.update(base64.b64decode(encrypted)) + dec.finalize()
    unpad = PKCS7(128).unpadder()
    return json.loads(unpad.update(padded) + unpad.finalize())


class AnycubicLANPrinter:
    def __init__(self, host):
        self.host = host
        self._connected = False
        self._client = None
        self._identity = None
        self.model_id = None
        self._state = {}
        self._lock = threading.RLock()
        self._callback = None
        self.last_report = 0
        self.last_info = 0

    def handshake(self):
        from core.webhook_utils import device_request
        response = device_request('GET', f'http://{self.host}:18910/info', timeout=10)
        response.raise_for_status()
        info = response.json()
        if info.get('ctrlType') != 'lan':
            raise ValueError('Printer LAN mode required')
        token = info['token']
        ctrl = urlsplit(info['ctrlInfoUrl'])
        # Never follow firmware-supplied destinations, credentials or redirects.
        if ctrl.scheme != 'http' or ctrl.port != 18910 or ctrl.username or ctrl.password or ctrl.path != '/ctrl':
            raise ValueError('Unsupported LAN handshake endpoint')
        ts, nonce = str(int(time.time() * 1000)), secrets.token_hex(3)
        # Anycubic LAN mandates MD5 for this protocol signature.
        first = hashlib.md5(token[:16].encode(), usedforsecurity=False).hexdigest()
        sign = hashlib.md5((first + ts + nonce).encode(), usedforsecurity=False).hexdigest()
        query = urlencode(dict(ts=ts, nonce=nonce, sign=sign, did=secrets.token_hex(16).upper()))
        response = device_request('POST', f'http://{self.host}:18910/ctrl?{query}', timeout=10)
        response.raise_for_status()
        result = response.json()
        if result.get('code') != 200:
            raise ValueError('LAN handshake rejected')
        creds = decrypt_handshake(token, result['data']['token'], result['data']['info'])
        self.model_id = str(info['modelId'])
        return creds

    def send_start(self, payload, *, before_publish, timeout=12):
        """Fresh TLS handshake, one QoS0 print/start, no reconnect or republish.

        A missing correlated report after publish is normal. Exceptions after
        entering publish retain an uncertain outcome so callers cannot replay.
        No raw handshake data, MQTT reports or exception text escape here.
        """
        if payload.get('type') != 'print' or payload.get('action') != 'start' or not callable(before_publish):
            raise ValueError('Only guarded print/start is allowed')
        client = None
        entered = published = False
        answer = {}
        def after_publish():
            """A missing report is normal; an explicit non-200 means the printer refused."""
            if answer.get('code') is not None and answer['code'] != 200:
                return dict(published=True, rejected=True, code=answer['code'], state=answer.get('state'), report_received=True,
                            message=f"Printer refused the start (code {answer['code']}, state {answer.get('state') or 'unknown'}); do not retry automatically")
            matched = bool(answer)
            return dict(published=True, report_received=matched,
                message='Published; correlated report received' if matched else
                        'Published; no correlated report. Do not retry automatically.')
        try:
            import paho.mqtt.client as mqtt
            from core.webhook_utils import device_connect_ip
            creds = self.handshake()
            if self.model_id != KOBRA_S1_MODEL_ID:
                return dict(published=False, uncertain=False, message='Printer does not report the Kobra S1 model; no command was sent')
            broker = urlsplit(creds['broker'])
            if broker.scheme not in ('mqtt', 'mqtts') or broker.port != 9883:
                raise ValueError('Unsupported LAN broker')
            address = device_connect_ip(self.host, 9883)
            model, device = self.model_id, creds['deviceId']
            topic = f'{PREFIX}/slicer/printer/{model}/{device}/print'
            report = f'{PREFIX}/printer/public/{model}/{device}/print/report'
            connected, subscribed, received = threading.Event(), threading.Event(), threading.Event()
            def on_connect(c, _u, _f, reason, _p=None):
                if not reason.is_failure:
                    connected.set()
                    c.subscribe(report, qos=0)
            def on_subscribe(_c, _u, _mid, reasons, _p=None):
                if reasons and all(not r.is_failure for r in reasons):
                    subscribed.set()
            def on_message(_c, _u, message):
                try:
                    body = json.loads(message.payload)
                    if message.topic == report and isinstance(body, dict) and body.get('msgid') == payload['msgid']:
                        # Only these two fields leave this method, and only in these shapes.
                        code, state = body.get('code'), body.get('state')
                        answer.update(code=code if type(code) is int else None,
                                      state=state if isinstance(state, str) and re.fullmatch(r'[A-Za-z0-9_ -]{1,32}', state) else None)
                        received.set()
                except (ValueError, TypeError):
                    pass
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            with tempfile.TemporaryDirectory(prefix='odin-s1-start-') as directory:
                paths = [os.path.join(directory, n) for n in ('cert.pem', 'key.pem')]
                for path, value in zip(paths, (creds['devicecrt'], creds['devicepk'])):
                    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
                        stream.write(value)
                context.load_cert_chain(*paths)
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                client_id='odin-s1-start-' + uuid.uuid4().hex[:8], protocol=mqtt.MQTTv311, reconnect_on_failure=False)
            client.username_pw_set(creds['username'], creds['password'])
            client.tls_set_context(context)
            client.on_connect, client.on_subscribe, client.on_message = on_connect, on_subscribe, on_message
            client.connect_timeout = timeout
            client.connect(address, 9883, keepalive=60)
            def wait(predicate):
                deadline = time.monotonic() + timeout
                while not predicate() and time.monotonic() < deadline:
                    if client.loop(timeout=min(.2, max(.01, deadline - time.monotonic()))) != mqtt.MQTT_ERR_SUCCESS:
                        return False
                return predicate()
            if not wait(connected.is_set) or not wait(subscribed.is_set):
                return dict(published=False, uncertain=False, message='S1 MQTT connection/subscription was not acknowledged')
            before_publish()
            entered = True
            sent = client.publish(topic, json.dumps(payload), qos=0, retain=False)
            if sent.rc != mqtt.MQTT_ERR_SUCCESS:
                return dict(published=False, uncertain=False, message='S1 MQTT publish was not accepted')
            if not wait(sent.is_published):
                return dict(published=False, uncertain=True, message='S1 MQTT delivery uncertain; do not retry automatically')
            published = True
            wait(received.is_set)
            return after_publish()
        except Exception as exc:
            from modules.printers.kobra_s1 import S1Refusal
            if not entered and isinstance(exc, S1Refusal):
                raise
            if published:
                return after_publish()
            return dict(published=False, uncertain=entered,
                message='S1 MQTT delivery uncertain; do not retry automatically' if entered else 'S1 LAN handshake or send preparation failed')
        finally:
            if client:
                try:
                    client.disconnect()
                except Exception:
                    pass

    def connect(self):
        self.disconnect()
        self.last_info = self.last_report = 0
        with self._lock:
            self._state['internal_state'] = 'OFFLINE'
        try:
            import paho.mqtt.client as mqtt
            from core.webhook_utils import device_connect_ip
            creds = self.handshake()  # Always fresh, including reconnects.
            broker = urlsplit(creds['broker'])
            if broker.scheme not in ('mqtt', 'mqtts') or broker.port != 9883:
                raise ValueError('Unsupported LAN broker')
            address = device_connect_ip(self.host, 9883)
            modes_to_try = [True, False] if creds.get('devicecrt') and creds.get('devicepk') else [False]
            for use_cert in modes_to_try:
                ready = threading.Event()
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f'odin-monitor-{uuid.uuid4().hex[:8]}', reconnect_on_failure=False)
                self._client = client
                self._identity = (self.model_id, creds['deviceId'])
                client.username_pw_set(creds['username'], creds['password'])
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                try:
                    if use_cert:
                        with tempfile.TemporaryDirectory(prefix='odin-anycubic-') as directory:
                            paths = [os.path.join(directory, name) for name in ('cert.pem', 'key.pem')]
                            for path, value in zip(paths, (creds['devicecrt'], creds['devicepk'])):
                                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                                with os.fdopen(fd, 'w') as stream:
                                    stream.write(value)
                            context.load_cert_chain(*paths)
                        # Context owns the cert now; files are already removed.
                    client.tls_set_context(context)
                    def connected(c, userdata, flags, reason, properties=None):
                        with self._lock:
                            if c is not self._client:
                                return
                            if not reason.is_failure:
                                c.subscribe(f'{PREFIX}/printer/public/{self.model_id}/{creds["deviceId"]}/#')
                                self._connected = True
                            ready.set()
                    client.on_connect = connected
                    client.on_disconnect = self._on_disconnect
                    client.on_message = self._on_message
                    client.connect(address, 9883, keepalive=60)
                    client.loop_start()
                    ready.wait(10)
                    if self._connected:
                        return True
                except Exception:
                    log.warning('Anycubic TLS connection attempt failed')
                self.disconnect()
        except Exception:
            # Never format transport errors, response objects, URLs or payloads.
            log.warning('Anycubic LAN connection failed')
        return False

    def disconnect(self):
        with self._lock:
            client, self._client = self._client, None
            self._connected = False
            self._identity = None
        if client:
            try:
                client.disconnect()
            except Exception:
                log.warning('Anycubic MQTT disconnect failed')
            finally:
                client.loop_stop()

    def _on_disconnect(self, client, *args):
        with self._lock:
            if client is self._client:
                self._connected = False

    def publish(self, kind, action):
        if action not in ALLOWED_ACTIONS:
            raise ValueError('Anycubic LAN adapter is monitor-only')
        if (kind, action) not in {('info', 'query'), ('tempature', 'query'), ('fan', 'query'), ('multiColorBox', 'getInfo')}:
            raise ValueError('Unsupported monitor query')
        with self._lock:
            if not self._connected or not self._client or not self._identity:
                return False
            model, device = self._identity
            body = dict(type=kind, action=action, timestamp=int(time.time() * 1000), msgid=str(uuid.uuid4()), data=None)
            return self._client.publish(f'{PREFIX}/web/printer/{model}/{device}/{kind}', json.dumps(body)).rc == 0

    def poll(self):
        return all([self.publish('info', 'query'), self.publish('multiColorBox', 'getInfo'),
                    self.publish('tempature', 'query'), self.publish('fan', 'query')])

    def _on_message(self, client, userdata, message):
        try:
            with self._lock:
                if client is not self._client or not self._identity:
                    return
                model, device = self._identity
                if not message.topic.startswith(f'{PREFIX}/printer/public/{model}/{device}/'):
                    return
                payload = json.loads(message.payload)
                if not valid_report(message.topic, payload):
                    return
                self._state = parse_report(message.topic, payload, self._state)
                self.last_report = time.monotonic()
                if message.topic.endswith('/info/report'):
                    self.last_info = self.last_report
                if self._callback:
                    self._callback(self.get_status())
        except Exception:
            log.warning('Anycubic report processing failed')

    def get_status(self):
        from copy import deepcopy
        with self._lock:
            return SimpleNamespace(**(deepcopy(DEFAULTS) | deepcopy(self._state)))

    def on_status(self, callback):
        self._callback = callback

    def get_webcam_url(self):
        return f'http://{self.host}:18088/flv'
