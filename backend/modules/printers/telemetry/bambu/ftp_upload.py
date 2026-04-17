"""Bambu FTPS file upload helper.

Bambu printers accept `.3mf` project files over implicit FTPS on port
990 (auth: user="bblp", password=access_code). This is separate from
MQTT — the command adapter handles MQTT; this module handles FTPS.

Extracted byte-for-byte from the legacy `BambuPrinter.upload_file`
method so dispatch.py can migrate without behavior change.
"""
from __future__ import annotations

# nosec B402 — FTPS (implicit TLS on port 990) is Bambu's published
# LAN file-transfer protocol. This is the same suppression the legacy
# adapter uses (backend/modules/printers/adapters/bambu.py).
import ftplib  # nosec B402
import logging
import os
import ssl

logger = logging.getLogger(__name__)

FTPS_PORT = 990


class _ImplicitFTPS(ftplib.FTP):
    """FTP subclass that does implicit-TLS on port 990.

    Python's stdlib `ftplib.FTP_TLS` does explicit TLS only (STARTTLS-style).
    Bambu uses implicit TLS — the socket is TLS from the first byte.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            value = context.wrap_socket(value, server_hostname=self.host)
        self._sock = value


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
    try:
        ftp = _ImplicitFTPS()
        ftp.connect(host=host, port=FTPS_PORT, timeout=timeout)
        ftp.login(user="bblp", passwd=access_code)
        ftp.set_pasv(True)
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {remote_filename}", f)
        ftp.quit()
        return True
    except Exception as e:
        logger.error("bambu ftps upload failed host=%s file=%s err=%s",
                     host, remote_filename, e)
        return False
