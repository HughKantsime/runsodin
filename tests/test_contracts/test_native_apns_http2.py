"""Real APNs provider over local TLS/HTTP2; no Apple credentials or devices."""
import datetime
import ipaddress
import json
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from modules.push.apns import APNsProvider


@pytest.mark.parametrize('status,accepted', [(200, True), (403, False)])
def test_real_tls_http2_apns_request(tmp_path, monkeypatch, status, accepted):
    # Import inside the test: missing production HTTP/2 extras must fail loudly.
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, RequestReceived, StreamEnded

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-datetime.timedelta(minutes=1))
            .not_valid_after(now+datetime.timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path = tmp_path/'local-cert.pem'
    key_path = tmp_path/'local-key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(pem)
    key_path.chmod(0o600)
    monkeypatch.setenv('SSL_CERT_FILE', str(cert_path))
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    monkeypatch.setenv('ODIN_ITAR_MODE', '0')
    monkeypatch.setenv('APNS_KEY_ID', 'local-test-key')
    monkeypatch.setenv('APNS_TEAM_ID', 'local-test-team')
    monkeypatch.setenv('APNS_KEY_CONTENT', pem.decode())
    monkeypatch.setenv('APNS_BUNDLE_ID', 'com.jlls.odin')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    context.set_alpn_protocols(['h2'])

    client_finished = threading.Event()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(5)

        def receive():
            raw, _ = listener.accept()
            with context.wrap_socket(raw, server_side=True) as connection:
                connection.settimeout(5)
                assert connection.selected_alpn_protocol() == 'h2'
                protocol = H2Connection(config=H2Configuration(
                    client_side=False, header_encoding='utf-8'))
                protocol.initiate_connection()
                connection.sendall(protocol.data_to_send())
                headers = {}
                body = bytearray()
                while True:
                    data = connection.recv(65535)
                    assert data, 'Provider disconnected before a complete request'
                    for event in protocol.receive_data(data):
                        if isinstance(event, RequestReceived):
                            headers = dict(event.headers)
                        elif isinstance(event, DataReceived):
                            body.extend(event.data)
                            protocol.acknowledge_received_data(
                                event.flow_controlled_length, event.stream_id)
                        elif isinstance(event, StreamEnded):
                            protocol.send_headers(event.stream_id,
                                [(':status', str(status))], end_stream=True)
                            connection.sendall(protocol.data_to_send())
                            # Closing with the client's frames unread resets the
                            # connection and can lose this response before it is read.
                            client_finished.wait(5)
                            return headers, json.loads(body)
                    pending = protocol.data_to_send()
                    if pending:
                        connection.sendall(pending)

        provider = APNsProvider()
        provider.host = f'https://127.0.0.1:{listener.getsockname()[1]}'
        with ThreadPoolExecutor(max_workers=1) as executor:
            response = executor.submit(receive)
            try:
                assert provider.send_push('0'*64, 'Local test', 'No real print',
                                          'PRINT_FAILED') is accepted
                client_finished.set()
                headers, body = response.result(timeout=6)
            finally:
                client_finished.set()
                provider.close()
        assert headers[':path'] == '/3/device/'+'0'*64
        assert headers['apns-topic'] == 'com.jlls.odin'
        assert headers['apns-push-type'] == 'alert'
        token = headers['authorization'].removeprefix('bearer ')
        assert jwt.get_unverified_header(token)['kid'] == 'local-test-key'
        claims = jwt.decode(token, key.public_key(), algorithms=['ES256'])
        assert claims['iss'] == 'local-test-team'
        assert body['aps']['category'] == 'PRINT_FAILED'
        assert body['aps']['alert']['body'] == 'No real print'
