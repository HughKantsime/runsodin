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
    try:
        ftp = _ImplicitFTPS()
        ftp.connect(host=host, port=FTPS_PORT, timeout=timeout)
        ftp.login(user="bblp", passwd=access_code)
        ftp.prot_p()
        ftp.set_pasv(True)
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {remote_filename}", f)
        ftp.quit()
        return True
    except Exception as e:
        logger.error("bambu ftps upload failed host=%s file=%s err=%s",
                     host, remote_filename, e)
        return False

    finally:
        if ftp is not None:
            ftp.close()
