"""Actual Paho MQTT 3.1.1 exchange with an owned loopback protocol peer."""
import json
import socketserver
import threading
import pytest

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("enterprise_license")

@pytest.fixture
def mqtt_peer(request,tmp_path):
    packets=[]
    disconnected=threading.Event()
    published=threading.Event()
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            def read_exact(size):
                data=b''
                while len(data)<size:
                    chunk=self.request.recv(size-len(data))
                    if not chunk:raise EOFError
                    data+=chunk
                return data
            try:
                while True:
                    header=read_exact(1)[0];length=0;multiplier=1
                    while True:
                        digit=read_exact(1)[0];length+=(digit&127)*multiplier
                        if not digit&128:break
                        multiplier*=128
                    body=read_exact(length);packets.append((header,body))
                    if header>>4==1:
                        self.request.sendall(b'\x20\x02\x00\x05' if getattr(request,'param',False) is True else b'\x20\x02\x00\x00')
                    elif header>>4==3:published.set()
                    elif header>>4==12:self.request.sendall(b'\xd0\x00')
                    elif header>>4==14:
                        disconnected.set();break
            except (EOFError,TimeoutError):pass
    config=getattr(request,'param',{})
    cert_path=None
    server_class=socketserver.ThreadingTCPServer
    if isinstance(config,dict) and config.get('tls'):
        import ssl,ipaddress
        from datetime import datetime,timedelta,timezone
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes,serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Synthetic MQTT peer')])
        san=x509.SubjectAlternativeName([x509.DNSName('mismatch.invalid')] if config.get('wrong_host') else [x509.IPAddress(ipaddress.ip_address('127.0.0.1'))])
        now=datetime.now(timezone.utc)
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1)).add_extension(san,critical=False).sign(key,hashes.SHA256()))
        cert_path=tmp_path/'synthetic-peer.pem';cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path=tmp_path/'synthetic-peer-key.pem';key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));key_path.chmod(0o600)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(str(cert_path),str(key_path))
        class TLSServer(socketserver.ThreadingTCPServer):
            def get_request(self):
                socket,address=super().get_request();socket.settimeout(3)
                try:return context.wrap_socket(socket,server_side=True),address
                except Exception:socket.close();raise
        server_class=TLSServer
    server=server_class(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:yield server.server_address[1],packets,disconnected,published,cert_path
    finally:server.shutdown();server.server_close();worker.join(timeout=2)


def test_diagnostic_real_connect_publish_disconnect_and_wire_payload(mqtt_peer,monkeypatch):
    from modules.notifications import mqtt_republish
    port,packets,disconnected,_,_=mqtt_peer
    monkeypatch.delenv('ODIN_ITAR_MODE',raising=False)
    result=mqtt_republish.test_connection('127.0.0.1',port,topic_prefix='synthetic/local/')
    assert result['success'] is True
    connects=[body for header,body in packets if header>>4==1]
    assert len(connects)==1 and connects[0][:7]==b'\x00\x04MQTT\x04'
    publications=[(header,body) for header,body in packets if header>>4==3]
    assert len(publications)==1
    header,body=publications[0];size=int.from_bytes(body[:2],'big')
    assert body[2:2+size]==b'synthetic/local/test'
    assert header&7==0
    payload=json.loads(body[2+size:]);assert payload['source']=='odin'
    assert payload['message']=='Connection test successful'
    assert disconnected.wait(timeout=2)
    assert any(header>>4==14 for header,body in packets)

@pytest.mark.parametrize('mqtt_peer',[True],indirect=True)
def test_real_broker_refusal_cannot_be_reported_as_success(mqtt_peer,monkeypatch):
    from modules.notifications import mqtt_republish
    port,packets,disconnected,_,_=mqtt_peer
    monkeypatch.delenv('ODIN_ITAR_MODE',raising=False)
    result=mqtt_republish.test_connection('127.0.0.1',port)
    assert result['success'] is False
    assert any(header>>4==1 for header,body in packets)

from tests.test_contracts.test_education_review_workflow import review_db

@pytest.mark.parametrize('mqtt_peer',[{}, {'tls':True}],indirect=True)
@pytest.mark.parametrize('authenticated',[False,True])
def test_configured_publisher_real_wire_and_synthetic_auth_fields(mqtt_peer,review_db,monkeypatch,authenticated):
    from sqlalchemy import text
    from modules.notifications import mqtt_republish
    port,packets,disconnected,published,certificate=mqtt_peer
    if certificate:monkeypatch.setenv('SSL_CERT_FILE',str(certificate))
    monkeypatch.delenv('ODIN_ITAR_MODE',raising=False)
    monkeypatch.setattr(mqtt_republish,'engine',review_db.get_bind())
    monkeypatch.setattr(mqtt_republish,'_client',None)
    monkeypatch.setattr(mqtt_republish,'_config_cache',None)
    monkeypatch.setattr(mqtt_republish,'_config_ts',0)
    values={'mqtt_republish_use_tls':'true' if certificate else 'false','mqtt_republish_enabled':'true','mqtt_republish_host':'127.0.0.1','mqtt_republish_port':str(port),'mqtt_republish_topic_prefix':'synthetic/publisher','mqtt_republish_username':'synthetic-user' if authenticated else '','mqtt_republish_password':'synthetic-noncredential' if authenticated else ''}
    for key,value in values.items():review_db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),{'key':key,'value':value})
    review_db.commit()
    try:
        mqtt_republish.republish_telemetry(9,'Synthetic Printer',{'progress':0,'temperature':21})
        assert published.wait(timeout=3)
        connect=next(body for header,body in packets if header>>4==1)
        assert bool(connect[7]&128) is authenticated
        assert bool(connect[7]&64) is authenticated
        fields=[];offset=10
        while offset<len(connect):
            size=int.from_bytes(connect[offset:offset+2],'big');offset+=2
            fields.append(connect[offset:offset+size].decode());offset+=size
        if authenticated:assert fields[1:]==['synthetic-user','synthetic-noncredential']
        else:assert len(fields)==1
        header,body=next((header,body) for header,body in packets if header>>4==3)
        size=int.from_bytes(body[:2],'big')
        assert body[2:2+size]==b'synthetic/publisher/synthetic_printer/status'
        payload=json.loads(body[2+size:]);assert payload['progress']==0 and payload['temperature']==21 and payload['printer_id']==9
        assert header&7==0
    finally:mqtt_republish.disconnect()
    assert disconnected.wait(timeout=2)
    assert mqtt_republish._client is None

@pytest.mark.parametrize('mqtt_peer,trusted,expected',[
    ({'tls':True},True,True),({'tls':True},False,False),
    ({'tls':True,'wrong_host':True},True,False),
],indirect=['mqtt_peer'])
def test_real_tls_trust_and_hostname_validation(mqtt_peer,monkeypatch,trusted,expected):
    from modules.notifications import mqtt_republish
    port,packets,disconnected,published,certificate=mqtt_peer
    monkeypatch.delenv('ODIN_ITAR_MODE',raising=False)
    if trusted:monkeypatch.setenv('SSL_CERT_FILE',str(certificate))
    else:monkeypatch.delenv('SSL_CERT_FILE',raising=False)
    response=mqtt_republish.test_connection('127.0.0.1',port,use_tls=True)
    assert response['success'] is expected
    if expected:
        assert published.wait(timeout=2) and disconnected.wait(timeout=2)
    else:assert packets==[]
