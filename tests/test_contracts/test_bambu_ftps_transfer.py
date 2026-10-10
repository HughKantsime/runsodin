"""Exercise both Bambu upload paths against a real local TLS FTP peer."""
import socket
import secrets
import ssl
import struct
import subprocess
import threading

import pytest

from modules.printers.telemetry.bambu import ftp_upload
from modules.printers.adapters import bambu


SYNTHETIC_FTPS_ACCESS_CODE = secrets.token_urlsafe(24)


def _server_context(root):
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(root / "key.pem"), "-out", str(root / "cert.pem"),
        "-days", "1", "-subj", "/CN=localhost",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "cert.pem", root / "key.pem")
    return context


@pytest.fixture(scope="module")
def server_context(tmp_path_factory):
    return _server_context(tmp_path_factory.mktemp("ftps-cert"))


@pytest.fixture(scope="module")
def resumption_context(tmp_path_factory):
    # Match the successful school transcript's negotiated TLS 1.2.
    context = _server_context(tmp_path_factory.mktemp("ftps-resumption-cert"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


class ProtectedFTPServer:
    def __init__(self, context, reject_protection=False, failure=None, expected_credentials=None, require_session_reuse=False):
        self.context = context
        self.require_session_reuse = require_session_reuse
        self.session_reused = False
        self.session_rejected = False
        self.failure = failure
        self.expected_credentials = expected_credentials
        self.reject_protection = reject_protection
        self.commands = []
        self.received = b""
        self.remote_filename = None
        self.data_tls = False
        self.errors = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(5)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        passive = None
        try:
            raw, _ = self.listener.accept()
            if self.failure == "control_tls":
                raw.sendall(b"220 Plaintext control rejected\r\n")
                raw.close()
                return
            with self.context.wrap_socket(raw, server_side=True) as control:
                control.settimeout(5)
                stream = control.makefile("r", encoding="latin-1")
                control.sendall(b"550 Greeting rejected\r\n" if self.failure == "greeting" else b"220 Local implicit FTPS\r\n")
                protected = False
                username = None
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    verb, _, argument = line.strip().partition(" ")
                    self.commands.append(verb + (" " + argument if verb in {"PBSZ", "PROT"} else ""))
                    if self.failure == "control_reset" and verb == "PROT":
                        control.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                        stream.close()
                        control.close()
                        return
                    if verb == "USER":
                        username = argument
                        response = "331 Password required"
                    elif verb == "PASS":
                        accepted = self.expected_credentials is None or (username, argument) == self.expected_credentials
                        response = "230 Logged in" if accepted else "530 Login rejected"
                    elif verb == "PBSZ":
                        response = "534 Buffer negotiation rejected" if self.failure == "pbsz" else "200 Buffer size accepted"
                    elif verb == "PROT":
                        protected = argument == "P" and not self.reject_protection and self.failure != "prot"
                        response = "200 Protected data accepted" if protected else "534 Protection rejected"
                    elif verb == "TYPE":
                        response = "200 Binary type"
                    elif verb == "PASV":
                        passive = socket.socket()
                        passive.bind(("127.0.0.1", 0))
                        passive.listen()
                        passive.settimeout(5)
                        port = passive.getsockname()[1]
                        response = f"227 Entering Passive Mode (127,0,0,1,{port // 256},{port % 256})"
                    elif verb == "STOR":
                        self.remote_filename = argument
                        if not protected:
                            response = "522 Data connections must be encrypted"
                        else:
                            control.sendall(b"150 Opening protected data connection\r\n")
                            data, _ = passive.accept()
                            data.settimeout(5)
                            if self.failure == "data_tls":
                                data.close()
                                control.sendall(b"426 Data TLS handshake rejected\r\n")
                                continue
                            with self.context.wrap_socket(data, server_side=True) as encrypted:
                                self.data_tls = isinstance(encrypted, ssl.SSLSocket)
                                self.session_reused = encrypted.session_reused
                                if self.failure == "session_reuse" or (self.require_session_reuse and not self.session_reused):
                                    self.session_rejected = True
                                    encrypted.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                                    encrypted.close()
                                    # Abort immediately without consuming data or sending 226.
                                    stream.close()
                                    return
                                chunks = []
                                while chunk := encrypted.recv(8192):
                                    chunks.append(chunk)
                                self.received = b"".join(chunks)
                                encrypted.unwrap().close()
                            if self.failure == "final_reply_reset":
                                control.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                                stream.close()
                                control.close()
                                return
                            response = "226 Transfer complete"
                    elif verb == "QUIT":
                        control.sendall(b"221 Goodbye\r\n")
                        break
                    else:
                        response = "502 Unsupported command"
                    control.sendall((response + "\r\n").encode("latin-1"))
                stream.close()
        except (OSError, ssl.SSLError) as error:
            self.errors.append(type(error).__name__)
        finally:
            if passive:
                passive.close()
            self.listener.close()

    def finish(self):
        self.thread.join(6)
        assert not self.thread.is_alive(), "FTPS connection was not closed"


def upload(path, peer, source, monkeypatch):
    if path == "v2":
        monkeypatch.setattr(ftp_upload, "FTPS_PORT", peer.port)
        return ftp_upload.upload_file("127.0.0.1", SYNTHETIC_FTPS_ACCESS_CODE, str(source), "test.3mf", timeout=3)
    printer = bambu.BambuPrinter.__new__(bambu.BambuPrinter)
    printer.ip = "127.0.0.1"
    printer.access_code = SYNTHETIC_FTPS_ACCESS_CODE
    printer.serial = "synthetic-test-printer"
    monkeypatch.setattr(ftp_upload, "FTPS_PORT", peer.port)
    return printer.upload_file(str(source), "test.3mf")


@pytest.mark.parametrize("path", ["legacy", "v2"])
def test_upload_negotiates_protected_tls_and_preserves_bytes(path, server_context, tmp_path, monkeypatch):
    source = tmp_path / "test.3mf"
    expected = b"PK\x03\x04synthetic-sliced-file\x00\xff" * 1024
    source.write_bytes(expected)
    peer = ProtectedFTPServer(server_context)
    try:
        assert upload(path, peer, source, monkeypatch)
    finally:
        peer.finish()
    assert not peer.errors
    assert peer.data_tls
    assert peer.received == expected
    assert peer.commands.index("PASS") < peer.commands.index("PBSZ 0") < peer.commands.index("PROT P") < peer.commands.index("STOR")
    assert "AUTH" not in peer.commands
    assert "PROT C" not in peer.commands


@pytest.mark.parametrize("path", ["legacy", "v2"])
def test_rejected_protection_never_uploads_or_falls_back(path, server_context, tmp_path, monkeypatch):
    source = tmp_path / "test.3mf"
    source.write_bytes(b"synthetic")
    peer = ProtectedFTPServer(server_context, reject_protection=True)
    try:
        assert not upload(path, peer, source, monkeypatch)
    finally:
        peer.finish()
    assert "PROT P" in peer.commands
    assert "STOR" not in peer.commands
    assert "PROT C" not in peer.commands
    assert not peer.received


@pytest.mark.parametrize("path", ["legacy", "v2"])
@pytest.mark.parametrize("failure", ["control_tls", "greeting", "pbsz", "prot", "data_tls", "session_reuse"])
def test_dispatch_transfer_failure_never_starts_print(path, failure, server_context, resumption_context, tmp_path, monkeypatch):
    from modules.printers import dispatch
    from modules.printers.telemetry.bambu import session

    def forbidden(*args, **kwargs):
        pytest.fail("Failed transfer must never connect MQTT or start printing")

    monkeypatch.setattr(dispatch, "_ws", lambda *args: None)
    monkeypatch.setattr(bambu.BambuPrinter, "connect", forbidden)
    monkeypatch.setattr(bambu.BambuPrinter, "start_print", forbidden)
    monkeypatch.setattr(session, "run_command", forbidden)
    source = tmp_path / "test.3mf"
    source.write_bytes(b"synthetic-dispatch-payload")
    peer = ProtectedFTPServer(resumption_context if failure == "session_reuse" else server_context,
                              failure=failure, require_session_reuse=failure == "session_reuse")
    monkeypatch.setattr(ftp_upload, "FTPS_PORT", peer.port)
    function = dispatch._dispatch_bambu_legacy if path == "legacy" else dispatch._dispatch_bambu_v2
    try:
        ok, reason = function(1, str(source), "test.3mf", {
            "ip": "127.0.0.1", "access_code": SYNTHETIC_FTPS_ACCESS_CODE, "serial": "synthetic-test-printer",
        })
        assert not ok
        assert "FTPS upload failed" in reason
    finally:
        peer.finish()
    assert "PROT C" not in peer.commands
    assert not peer.received
    assert not peer.errors
    if failure not in {"data_tls", "session_reuse"}:
        assert "STOR" not in peer.commands


@pytest.mark.parametrize("path", ["legacy", "v2"])
@pytest.mark.parametrize("reuse_control_session", [False, True])
def test_strict_tls12_peer_requires_production_session_reuse(path, reuse_control_session,
                                                            resumption_context, tmp_path, monkeypatch, caplog):
    """Real production success; intentionally broken baseline proves peer enforcement."""
    import ftplib
    if not reuse_control_session:
        def unresumed_transfer(self, cmd, rest=None):
            if not self._prot_p:
                raise ftplib.error_perm("Protected data transfer was not negotiated")
            conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
            try:
                return self.context.wrap_socket(conn, server_hostname=self.host), size
            except Exception:
                conn.close()
                raise
        monkeypatch.setattr(ftp_upload._ImplicitFTPS, "ntransfercmd", unresumed_transfer)
    source = tmp_path / "resumption.3mf"
    payload = b"PK\x03\x04synthetic-tls-resumption-payload" * 1024
    source.write_bytes(payload)
    peer = ProtectedFTPServer(resumption_context, require_session_reuse=True,
                              expected_credentials=("bblp", SYNTHETIC_FTPS_ACCESS_CODE))
    try:
        result = upload(path, peer, source, monkeypatch)
    finally:
        peer.finish()
    assert not peer.errors
    assert peer.data_tls
    assert "PROT P" in peer.commands and "PROT C" not in peer.commands
    assert result is reuse_control_session
    if not reuse_control_session:
        # Abortive peer close can surface on write (EPIPE) or read (ECONNRESET).
        assert any(any(message in record.getMessage() for message in
                       ("Connection reset by peer", "Broken pipe")) for record in caplog.records)
    assert peer.session_reused is reuse_control_session
    assert peer.session_rejected is not reuse_control_session
    assert peer.received == (payload if reuse_control_session else b"")
