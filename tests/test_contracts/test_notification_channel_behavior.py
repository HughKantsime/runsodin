"""Notification SMTP transport and Web Push provider-response lifecycle."""
import json
import socketserver
import threading
from email import policy
from email.parser import BytesParser
import pytest
import requests
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def channels_db(review_db, monkeypatch):
    from core import db_utils
    from modules.notifications import channels
    monkeypatch.setattr(db_utils, "engine", review_db.get_bind())
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    return channels

@pytest.fixture
def smtp_peer():
    received = []
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(5)
            self.wfile.write(b"220 synthetic.test ESMTP ready\r\n")
            data = []
            collecting = False
            while True:
                line = self.rfile.readline()
                if not line: break
                if collecting:
                    if line == b".\r\n":
                        received.append(b"".join(data))
                        collecting = False
                        self.wfile.write(b"250 accepted\r\n")
                    else: data.append(line[1:] if line.startswith(b"..") else line)
                    continue
                command = line.split(b" ",1)[0].strip().upper()
                if command in (b"EHLO",b"HELO"):
                    self.wfile.write(b"250 synthetic.test\r\n")
                elif command == b"DATA":
                    collecting = True
                    self.wfile.write(b"354 send data\r\n")
                elif command == b"QUIT":
                    self.wfile.write(b"221 closing\r\n")
                    break
                else: self.wfile.write(b"250 OK\r\n")
    server = socketserver.ThreadingTCPServer(("127.0.0.1",0),Handler)
    worker = threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try: yield server.server_address[1], received
    finally: server.shutdown();server.server_close();worker.join(timeout=2)


def smtp_config(review_db, port, enabled=True):
    settings = {"enabled":enabled,"host":"127.0.0.1","port":port,"use_tls":False,"from_address":"odin@example.test"}
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('smtp_config',:value)"),{"value":json.dumps(settings)})
    review_db.commit()


def test_email_delivers_multipart_and_escapes_untrusted_text(channels_db, review_db, smtp_peer, pro_license):
    # Alert email needs Pro (license_manager.TIERS email_notifications).
    port, received = smtp_peer
    smtp_config(review_db,port)
    channels_db.send_email(1,"job_completed","<b>Fixture title</b>","<img src=x onerror=evil()> & details")
    assert len(received) == 1
    message = BytesParser(policy=policy.default).parsebytes(received[0])
    assert message["To"] == "student@example.test"
    assert message["From"] == "odin@example.test"
    assert "<b>Fixture title</b>" in str(message["Subject"])
    assert "<img src=x onerror=evil()>" in message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "<img src=x onerror=evil()>" not in html
    assert "&lt;img src=x onerror=evil()&gt; &amp; details" in html
    assert "&lt;b&gt;Fixture title&lt;/b&gt;" in html


def test_disabled_email_does_not_connect(channels_db, review_db, smtp_peer):
    port, received = smtp_peer
    smtp_config(review_db,port,enabled=False)
    channels_db.send_email(1,"job_completed","Fixture","Disabled")
    assert received == []


@pytest.mark.parametrize("status,remaining", [(404,0),(410,0),(503,1)])
def test_webpush_removes_only_expired_subscriptions(channels_db,review_db,monkeypatch,status,remaining):
    import pywebpush
    review_db.execute(text("INSERT INTO push_subscriptions(user_id,endpoint,p256dh_key,auth_key) VALUES(1,'https://push.example.test/fixture','fixture-key','fixture-auth')"))
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('vapid_keys',:value)"),{"value":json.dumps({"private_key":"synthetic-noncredential"})})
    review_db.commit()
    calls = []
    def webpush(**kwargs):
        calls.append(kwargs)
        response = requests.Response();response.status_code=status
        raise pywebpush.WebPushException("Synthetic provider failure",response=response)
    monkeypatch.setattr(pywebpush,"webpush",webpush)
    channels_db.send_push_notification(1,"job_completed","Fixture","Test")
    assert len(calls) == 1
    payload = json.loads(calls[0]["data"])
    assert payload["alert_type"] == "job_completed"
    with review_db.get_bind().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM push_subscriptions WHERE user_id=1")).scalar_one() == remaining
