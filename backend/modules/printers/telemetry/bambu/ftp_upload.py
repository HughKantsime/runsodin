"""Bambu FTPS file upload helper.

Bambu printers accept `.3mf` project files over implicit FTPS on port
990 (auth: user="bblp", password=access_code). This is separate from
MQTT — the command adapter handles MQTT; this module handles FTPS.

Shared by legacy and V2 dispatch so both negotiate protected data transfer.
"""
from __future__ import annotations

# nosec B402 — FTPS (implicit TLS on port 990) is Bambu's published
# LAN file-transfer protocol. This is the same suppression the legacy
# adapter uses (backend/modules/printers/adapters/bambu.py).
import contextlib
import contextvars
from dataclasses import dataclass
import fcntl
import ftplib  # nosec B402
import logging
import os
import socket
import ssl
import time

logger = logging.getLogger(__name__)

FTPS_PORT = 990


class _ImplicitFTPS(ftplib.FTP_TLS):
    """Implicit TLS control connection with protected TLS data connections."""

    def __init__(self):
        # Bambu LAN certificates are self-signed; retain existing trust policy.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(context=context)

    def connect(self, host="", port=FTPS_PORT, timeout=30.0):
        self.host, self.port, self.timeout = host, port, timeout
        # Connect to the policy-validated IP literal; TLS keeps the original
        # name as server_hostname (certificate checks are off for Bambu's
        # self-signed LAN certificates). PASV data connections use the
        # control peer address (ftplib trust_server_pasv_ipv4_address=False).
        from core.webhook_utils import device_connect_ip
        raw = socket.create_connection((device_connect_ip(host, port), port), timeout=timeout)
        try:
            self.sock = self.context.wrap_socket(raw, server_hostname=host)
            self.af = self.sock.family
            self.file = self.sock.makefile("r", encoding=self.encoding)
            self.welcome = self.getresp()
            return self.welcome
        except Exception:
            raw.close()
            self.close()
            raise

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        """Protected STOR with bounded shutdown and explicit FTP completion.

        Some Bambu peers acknowledge the transfer only after data TCP closes,
        while Python's inherited uploader waits for peer TLS close_notify.
        Sending our close_notify remains mandatory; a timeout at that point
        is not success. The control channel must still acknowledge 226/250.
        """
        if not self._prot_p:
            raise ftplib.error_perm("Protected data transfer was not negotiated")
        self.voidcmd("TYPE I")
        connection=self.transfercmd(cmd,rest)
        try:
            while True:
                chunk=fp.read(blocksize)
                if not chunk:break
                connection.sendall(chunk)
                if callback:callback(chunk)
            timeout=connection.gettimeout()
            connection.settimeout(1.0 if timeout is None else min(timeout,1.0))
            try:
                plain=connection.unwrap()
            except TimeoutError:
                # All bytes were sent; close TCP and require control completion.
                pass
            else:
                plain.close()
        finally:
            connection.close()
        response=self.voidresp()
        if response[:3] not in ("226","250"):
            raise ftplib.error_reply("Transfer completion unconfirmed")
        return response

    def ntransfercmd(self, cmd, rest=None):
        if not self._prot_p:
            raise ftplib.error_perm("Protected data transfer was not negotiated")
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        try:
            # Support printer FTP servers requiring the data connection to
            # resume the control session; sharing only SSLContext is insufficient.
            return self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session,
            ), size
        except Exception:
            conn.close()
            raise


# One Bambu FTPS upload at a time across the install (specs/ftps-upload-serialization.md):
# on 2026-11-08 an overlapping upload on a weak access point timed out mid-transfer.
DEFAULT_WAIT_SECONDS = 900
MAX_WAIT_SECONDS = 3600
POLL_SECONDS = 0.5
WAIT_NOTICE_SECONDS = 2.0


@dataclass(frozen=True)
class UploadResult:
    """Truthy only when the file arrived. ``phase`` names where a failure happened;
    ``wait`` means the slot never came free and no connection was attempted."""
    ok: bool
    phase: str
    waited_seconds: float = 0.0

    def __bool__(self) -> bool:
        return self.ok


_wait_notice = contextvars.ContextVar("bambu_ftps_wait_notice", default=None)


@contextlib.contextmanager
def wait_notice(callback):
    """Call ``callback`` once if an upload in this context waits for the slot."""
    token = _wait_notice.set(callback)
    try:
        yield
    finally:
        _wait_notice.reset(token)


def _serial_enabled() -> bool:
    return os.getenv("ODIN_FTPS_UPLOAD_SERIAL", "1").strip().lower() not in {"0", "false", "no", "off"}


def _wait_limit() -> float:
    try:
        value = float(os.getenv("ODIN_FTPS_UPLOAD_WAIT_SECONDS", DEFAULT_WAIT_SECONDS))
    except ValueError:
        return float(DEFAULT_WAIT_SECONDS)
    if value != value:  # NaN
        return float(DEFAULT_WAIT_SECONDS)
    return min(max(value, 0.0), float(MAX_WAIT_SECONDS))


def _lock_path() -> str:
    explicit = os.getenv("ODIN_FTPS_UPLOAD_LOCK_PATH")
    if explicit:
        return explicit
    return os.path.join(os.getenv("ODIN_DATA_DIR", "/data"), "locks", "bambu-ftps-upload.lock")


@contextlib.contextmanager
def upload_slot(on_wait=None, *, clock=time.monotonic, sleep=time.sleep):
    """Hold the install-wide FTPS upload slot; yields seconds waited, or None on timeout.

    flock on a private descriptor excludes other processes and other threads alike,
    and the kernel releases it if the holder dies. If the lock file can't be opened
    the upload proceeds unserialized (logged): the gate protects throughput, not safety.
    """
    if not _serial_enabled():
        yield 0.0
        return
    path = _lock_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        logger.warning("bambu ftps upload lock unavailable error_type=%s; uploading unserialized", type(exc).__name__)
        yield 0.0
        return
    try:
        started = clock()
        limit = _wait_limit()
        noticed = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = clock() - started
                if waited >= limit:
                    yield None
                    return
                if on_wait is not None and not noticed and waited >= WAIT_NOTICE_SECONDS:
                    noticed = True
                    try:
                        on_wait()
                    except Exception:
                        logger.debug("ftps upload wait notice failed", exc_info=True)
                sleep(POLL_SECONDS)
        try:
            yield clock() - started
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def upload_file(
    host: str,
    access_code: str,
    local_path: str,
    remote_filename: str | None = None,
    timeout: float = 30.0,
) -> UploadResult:
    """Upload a local file to the printer's FTPS root via implicit TLS, inside the
    install-wide upload slot. The result is truthy on success; failures are logged."""
    with upload_slot(_wait_notice.get()) as waited:
        if waited is None:
            logger.error("bambu ftps upload failed phase=wait error_type=SlotTimeout")
            return UploadResult(False, "wait", _wait_limit())
        ok, phase = _upload_once(host, access_code, local_path, remote_filename, timeout)
        return UploadResult(ok, phase, waited)


def _upload_once(host, access_code, local_path, remote_filename, timeout):
    if remote_filename is None:
        remote_filename = os.path.basename(local_path)
    ftp = None
    phase="connect"
    try:
        ftp = _ImplicitFTPS()
        ftp.connect(host=host, port=FTPS_PORT, timeout=timeout)
        phase="authentication"
        ftp.login(user="bblp", passwd=access_code)
        phase="protection"
        ftp.prot_p()
        ftp.set_pasv(True)
        phase="transfer"
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {remote_filename}", f)
        phase="logout"
        ftp.quit()
        return True, "done"
    except Exception as e:
        logger.error("bambu ftps upload failed phase=%s error_type=%s",phase,type(e).__name__)
        return False, phase

    finally:
        if ftp is not None:
            ftp.close()
