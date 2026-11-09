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
import threading
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
DEFAULT_MAX_WAITERS = 2  # of 8 dispatch workers; the rest stay free for other printers
MAX_WAIT_SECONDS = 3600
POLL_SECONDS = 0.5
WAIT_NOTICE_SECONDS = 2.0


@dataclass(frozen=True)
class UploadResult:
    """Truthy only when the file arrived. ``phase`` names where a failure happened;
    ``timeout``, ``busy`` and ``lock_unavailable`` mean no connection was attempted.
    Returned (rather than a bare bool) so existing ``if not upload_file(...)``
    callers and test doubles keep working while dispatch reads the details."""
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


_waiters_lock = threading.Lock()
_waiters = 0


def _max_waiters() -> int:
    try:
        return max(0, int(os.getenv("ODIN_FTPS_UPLOAD_MAX_WAITERS", DEFAULT_MAX_WAITERS)))
    except ValueError:
        return DEFAULT_MAX_WAITERS


@contextlib.contextmanager
def upload_slot(on_wait=None, *, clock=time.monotonic, sleep=time.sleep):
    """Hold the install-wide FTPS upload slot. Yields ``(state, waited_seconds)``.

    ``state`` is ``"held"`` (upload may proceed), ``"timeout"`` (waited the full
    limit), ``"busy"`` (too many uploads already waiting in this process, so a
    dispatch worker is not tied up), or ``"lock_unavailable"`` (the lock file
    could not be opened; only ``ODIN_FTPS_UPLOAD_SERIAL=0`` bypasses the slot).
    flock on a private descriptor excludes other processes and other threads
    alike, and the kernel releases it if the holder dies.
    """
    global _waiters
    if not _serial_enabled():
        yield "held", 0.0
        return
    path = _lock_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        logger.error("bambu ftps upload lock unavailable error_type=%s", type(exc).__name__)
        yield "lock_unavailable", 0.0
        return
    waiting = False
    try:
        started = clock()
        limit = _wait_limit()
        noticed = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if not waiting:
                    with _waiters_lock:
                        full = _waiters >= _max_waiters()
                        if not full:
                            _waiters += 1
                    if full:
                        yield "busy", 0.0
                        return
                    waiting = True
                waited = clock() - started
                if waited >= limit:
                    yield "timeout", waited
                    return
                if on_wait is not None and not noticed and waited >= WAIT_NOTICE_SECONDS:
                    noticed = True
                    try:
                        on_wait()
                    except Exception:
                        logger.debug("ftps upload wait notice failed", exc_info=True)
                sleep(max(0.0, min(POLL_SECONDS, limit - waited)))
                continue
            waited = clock() - started
            if waiting and waited > limit:
                # Acquired only after the deadline: give the slot back unused.
                fcntl.flock(fd, fcntl.LOCK_UN)
                yield "timeout", waited
                return
            break
        if waiting:
            with _waiters_lock:
                _waiters -= 1
            waiting = False
        try:
            yield "held", waited
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if waiting:
            with _waiters_lock:
                _waiters -= 1
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
    with upload_slot(_wait_notice.get()) as (state, waited):
        if state != "held":
            logger.error("bambu ftps upload failed phase=%s error_type=Slot", state)
            return UploadResult(False, state, waited)
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
