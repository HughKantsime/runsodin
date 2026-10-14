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
import ftplib  # nosec B402
import logging
import os
import socket
import ssl

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
        raw = socket.create_connection((host, port), timeout=timeout)
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


def upload_file(
    host: str,
    access_code: str,
    local_path: str,
    remote_filename: str | None = None,
    timeout: float = 30.0,
) -> bool:
    """Upload a local file to the printer's FTPS root via implicit TLS.

    Returns True on success, False on any error (logged).
    """
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
        return True
    except Exception as e:
        logger.error("bambu ftps upload failed phase=%s error_type=%s",phase,type(e).__name__)
        return False

    finally:
        if ftp is not None:
            ftp.close()
