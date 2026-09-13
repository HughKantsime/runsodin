"""Peer-pinned Bambu active session, imported only by the exercise worker."""

from __future__ import annotations

# Certification uses an implicit-TLS FTP_TLS subclass, PROT P, and pinned control/data peers.
import ftplib  # nosec B402
import ipaddress
import json
import socket
import ssl
import threading
import time
from collections import deque
import paho.mqtt.client as mqtt

from .active import ExerciseError, Observation
from .active_backends import BambuExerciseBackend
from .active_transports import BambuActiveTransport
from .assets import ValidatedAsset
from .config import ResolvedTarget
from .passive.parsers import ParseError, parse_bambu_sample
from .passive.transports import PassivePolicyError, decode_bounded_json_object
from modules.printers.telemetry.state import PrinterStatus


WAIT_SECONDS = 15.0
MAX_PAYLOAD = 262_144


def _same_address(left: str, right: str) -> bool:
    return ipaddress.ip_address(left.split("%", 1)[0]) == ipaddress.ip_address(right.split("%", 1)[0])


def _insecure_local_tls() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _close_mqtt_client(client) -> None:
    """Attempt both MQTT teardown operations and report only after both ran."""
    failures: list[BaseException] = []
    for operation in (client.disconnect, client.loop_stop):
        try:
            operation()
        except BaseException as exc:
            failures.append(exc)
    if failures:
        raise ExerciseError("Bambu MQTT cleanup failed") from failures[0]


class _PinnedImplicitFtps(ftplib.FTP_TLS):
    def __init__(self, *, logical_host: str, address: str, context: ssl.SSLContext, timeout: float):
        super().__init__(context=context, timeout=timeout)
        self.__logical_host = logical_host
        self.__address = address

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        chosen_timeout = self.timeout if timeout == -999 else timeout
        self.host = self.__logical_host
        self.port = port
        self.sock = socket.create_connection((self.__address, port), chosen_timeout, source_address)
        if not _same_address(self.sock.getpeername()[0], self.__address):
            self.sock.close()
            raise ExerciseError("FTPS control peer changed")
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.__logical_host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def makepasv(self):
        _advertised_host, port = super().makepasv()
        if not 1 <= port <= 65535:
            raise ExerciseError("FTPS passive port is invalid")
        return self.__address, port

    def ntransfercmd(self, cmd, rest=None):
        connection, size = super().ntransfercmd(cmd, rest)
        if not _same_address(connection.getpeername()[0], self.__address):
            connection.close()
            raise ExerciseError("FTPS data peer changed")
        return connection, size


class BambuLiveSession:
    def __init__(self, target: ResolvedTarget, remote_name: str, asset_path: ValidatedAsset | None):
        self.__target = target
        self.__remote_name = remote_name
        self.__asset = asset_path
        self.__condition = threading.Condition()
        self.__messages: deque[bytes] = deque(maxlen=32)
        self.__failed = False
        self.__status = PrinterStatus.initial()
        connection = target.connection
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        client.username_pw_set("bblp", connection["access_code"])
        client.tls_set_context(_insecure_local_tls())

        def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
            numeric = reason_code.value if hasattr(reason_code, "value") else reason_code
            if numeric != 0:
                with self.__condition:
                    self.__failed = True
                    self.__condition.notify_all()
                return
            _client.subscribe(f"device/{connection['device_token']}/report", qos=0)

        def on_message(_client, _userdata, message):
            with self.__condition:
                if message.topic != f"device/{connection['device_token']}/report" or len(message.payload) > MAX_PAYLOAD:
                    self.__failed = True
                else:
                    self.__messages.append(bytes(message.payload))
                self.__condition.notify_all()

        client.on_connect = on_connect
        client.on_message = on_message
        self.__client = client
        try:
            client.connect(target.address, connection["port"], keepalive=30)
            client.loop_start()
        except BaseException:
            try:
                _close_mqtt_client(client)
            except BaseException:
                pass
            raise

    def observe(self) -> Observation:
        deadline = time.monotonic() + WAIT_SECONDS
        with self.__condition:
            while not self.__messages and not self.__failed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ExerciseError("Bambu status observation timed out")
                self.__condition.wait(remaining)
            if self.__failed:
                raise ExerciseError("Bambu telemetry session failed")
            payload = self.__messages.popleft()
        try:
            bounded = decode_bounded_json_object(payload)
            sample, self.__status = parse_bambu_sample(bounded, previous=self.__status, timestamp=time.time())
        except (ParseError, PassivePolicyError) as exc:
            raise ExerciseError("Bambu status parsing failed") from exc
        return Observation(sample.state, sample.filename, sample.job_id, sample.ams_slots)

    def __publish_closed(self, payload: dict) -> bool:
        connection = self.__target.connection
        topic = f"device/{connection['device_token']}/request"
        sender = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311)
        sender.username_pw_set("bblp", connection["access_code"])
        sender.tls_set_context(_insecure_local_tls())
        connected = threading.Event()

        def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
            numeric = reason_code.value if hasattr(reason_code, "value") else reason_code
            if numeric == 0:
                connected.set()

        sender.on_connect = on_connect
        primary_failure: BaseException | None = None
        try:
            sender.connect(self.__target.address, connection["port"], keepalive=30)
            sender.loop_start()
            if not connected.wait(WAIT_SECONDS):
                return False
            receipt = sender.publish(topic, payload=json.dumps(payload, separators=(",", ":")), qos=0)
            receipt.wait_for_publish(timeout=WAIT_SECONDS)
            return receipt.rc == mqtt.MQTT_ERR_SUCCESS and receipt.is_published()
        except BaseException as exc:
            primary_failure = exc
            raise
        finally:
            try:
                _close_mqtt_client(sender)
            except BaseException:
                if primary_failure is None:
                    raise

    def upload(self, remote_name: str) -> bool:
        if self.__asset is None or remote_name != self.__remote_name:
            return False
        connection = self.__target.connection
        ftp = _PinnedImplicitFtps(
            logical_host=connection["host"], address=self.__target.address,
            context=_insecure_local_tls(), timeout=WAIT_SECONDS,
        )
        primary_failure: BaseException | None = None
        try:
            ftp.connect(connection["host"], connection.get("ftps_port", 990))
            ftp.login("bblp", connection["access_code"])
            ftp.prot_p()
            with self.__asset.open() as stream:
                response = ftp.storbinary(f"STOR {remote_name}", stream, blocksize=64 * 1024)
            return response.startswith("2")
        except BaseException as exc:
            primary_failure = exc
            raise
        finally:
            try:
                ftp.quit()
            except BaseException as quit_failure:
                try:
                    ftp.close()
                except BaseException:
                    pass
                if primary_failure is None:
                    raise ExerciseError("Bambu FTPS cleanup failed") from quit_failure

    def close(self) -> None:
        _close_mqtt_client(self.__client)

    def backend(self) -> BambuExerciseBackend:
        transport = BambuActiveTransport(
            expected_remote_name=self.__remote_name,
            publisher=self.__publish_closed, uploader=self.upload,
        )
        return BambuExerciseBackend(self.observe, transport, self.close)
