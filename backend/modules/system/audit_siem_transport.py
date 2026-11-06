"""Pooled HTTP and one-event syslog batches with bounded close detection."""
import hashlib
import hmac
import socket
import ssl
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlsplit


class Transport:
    def __init__(self):
        self.session = None
        self.socket = None
        self.address = None
        self.ca = None

    def close(self):
        for resource in (self.session, self.socket, self.ca):
            if resource is not None:
                resource.close()

    def __call__(self, destination, body, event_id):
        from core.crypto import decrypt
        from core.webhook_utils import device_connect_ip, pinned_device_destination
        from core.itar import enforce_host_destination
        parsed = urlsplit(destination['endpoint'])
        if destination['kind'] == 'webhook':
            # Revalidate DNS and ITAR on every delivery, including pooled connections.
            with pinned_device_destination(destination['endpoint']) as ips:
                for ip in ips:
                    enforce_host_destination(ip)
                address = tuple(ips)
                if self.address is not None and address != self.address:
                    self.session.close()
                    self.session = None
                self.address = address
                secret = decrypt(destination['secret'])
                if not secret:
                    raise ValueError('Webhook signing secret unavailable')
                signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                if self.session is None:
                    import requests
                    self.session = requests.Session()
                    self.session.trust_env = False
                verify = True
                if destination.get('ca_bundle'):
                    if self.ca is None:
                        self.ca = tempfile.NamedTemporaryFile(mode='w', suffix='.pem')
                        self.ca.write(destination['ca_bundle'])
                        self.ca.flush()
                    verify = self.ca.name
                response = self.session.post(destination['endpoint'], data=body,
                    headers={'Content-Type': 'application/json', 'X-ODIN-Signature': 'sha256='+signature,
                             'X-ODIN-Event-ID': event_id}, timeout=10, stream=True,
                    allow_redirects=False, verify=verify)
                try:
                    if not 200 <= response.status_code < 300:
                        raise ValueError('SIEM HTTP '+str(response.status_code))
                    # Drain only a bounded response so ordinary keep-alive can reuse TCP.
                    size = 0
                    for chunk in response.iter_content(8192):
                        size += len(chunk)
                        if size > 65536:
                            break
                finally:
                    response.close()
        else:
            port = parsed.port or (6514 if destination['tls'] else 514)
            ip = device_connect_ip(parsed.hostname, port)
            enforce_host_destination(ip)
            if self.socket is not None and self.address != (ip, port):
                self.socket.close()
                self.socket = None
            if self.socket is None:
                raw = socket.create_connection((ip, port), timeout=10)
                try:
                    context = (ssl.create_default_context(cadata=destination['ca_bundle'])
                               if destination.get('ca_bundle') else ssl.create_default_context())
                    self.socket = context.wrap_socket(raw, server_hostname=parsed.hostname) if destination['tls'] else raw
                except BaseException:
                    raw.close()
                    raise
                self.address = (ip, port)
            timestamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            message = f'<134>1 {timestamp} odin odin-audit - {event_id} - '.encode()+body
            # RFC5424 over RFC6587 octet-counted TCP; no delimiter injection.
            try:
                self.socket.sendall(str(len(message)).encode()+b' '+message)
                # Syslog has no application ACK. Each event is its own reconnecting
                # batch: observe FIN/RST/TLS close before accepting local delivery.
                self.socket.settimeout(0.05)
                try:
                    if self.socket.recv(1) == b'':
                        raise ConnectionError('Syslog receiver closed the connection')
                except socket.timeout:
                    pass
            finally:
                self.socket.close()
                self.socket = None


def send(destination, body, event_id):
    transport = Transport()
    try:
        transport(destination, body, event_id)
    finally:
        transport.close()
