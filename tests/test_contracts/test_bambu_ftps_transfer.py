"""Exercise both Bambu upload paths against a real local TLS FTP peer."""
import socket
import ssl
import subprocess
import threading

import pytest

from modules.printers.telemetry.bambu import ftp_upload
from modules.printers.adapters import bambu


@pytest.fixture(scope="module")
def server_context(tmp_path_factory):
    root = tmp_path_factory.mktemp("ftps-cert")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(root / "key.pem"), "-out", str(root / "cert.pem"),
        "-days", "1", "-subj", "/CN=localhost",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "cert.pem", root / "key.pem")
    return context


class ProtectedFTPServer:
    def __init__(self, context, reject_protection=False, failure=None):
        self.context = context
        self.failure = failure
        self.reject_protection = reject_protection
        self.commands = []
        self.received = b""
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
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    verb, _, argument = line.strip().partition(" ")
                    self.commands.append(verb + (" " + argument if verb in {"PBSZ", "PROT"} else ""))
                    if verb == "USER":
                        response = "331 Password required"
                    elif verb == "PASS":
                        response = "230 Logged in"
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
                                chunks = []
                                while chunk := encrypted.recv(8192):
                                    chunks.append(chunk)
                                self.received = b"".join(chunks)
                                encrypted.unwrap().close()
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
        return ftp_upload.upload_file("127.0.0.1", "synthetic-test-code", str(source), "test.3mf", timeout=3)
    printer = bambu.BambuPrinter.__new__(bambu.BambuPrinter)
    printer.ip = "127.0.0.1"
    printer.access_code = "synthetic-test-code"
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
@pytest.mark.parametrize("failure", ["control_tls", "greeting", "pbsz", "prot", "data_tls"])
def test_dispatch_transfer_failure_never_starts_print(path, failure, server_context, tmp_path, monkeypatch):
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
    peer = ProtectedFTPServer(server_context, failure=failure)
    monkeypatch.setattr(ftp_upload, "FTPS_PORT", peer.port)
    function = dispatch._dispatch_bambu_legacy if path == "legacy" else dispatch._dispatch_bambu_v2
    try:
        ok, reason = function(1, str(source), "test.3mf", {
            "ip": "127.0.0.1", "access_code": "synthetic-test-code", "serial": "synthetic-test-printer",
        })
        assert not ok
        assert "FTPS upload failed" in reason
    finally:
        peer.finish()
    assert "PROT C" not in peer.commands
    assert not peer.received
    assert not peer.errors
    if failure != "data_tls":
        assert "STOR" not in peer.commands
