"""Certification group F / cluster A support: outermost-boundary stubs (no tests here).

* ``StubSMTP``  - a minimal RFC 5321 server on a loopback port. It accepts EHLO/HELO, MAIL, RCPT,
  DATA, RSET, NOOP and QUIT (no STARTTLS/AUTH) and records every accepted message, so ODIN's real
  ``smtplib`` client code is exercised over a real socket.
* ``StubIdP``   - a minimal OpenID Connect provider shaped like Microsoft Entra ID v2.0
  (tenant-scoped discovery document, token endpoint, JWKS and userinfo). ID tokens are RS256
  signed with a key generated per instance. Every request is recorded.

All key material and identities are generated per instance and never written to receipts.
"""
from __future__ import annotations

import email
import json
import secrets
import socket
import threading
import time
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------------------------
# SMTP
# --------------------------------------------------------------------------------------------
class StubSMTP:
    def __init__(self):
        self.port = _free_port()
        self.messages: list[dict] = []
        self.sessions = 0
        self._lock = threading.Lock()
        self._sock = None
        self._stop = threading.Event()

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        if self._sock:
            self._sock.close()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with self._lock:
            self.sessions += 1
        f = conn.makefile("rb")

        def send(line: str):
            conn.sendall((line + "\r\n").encode())

        send("220 stub.smtp.test ESMTP certification stub")
        mail_from, rcpts = None, []
        try:
            while True:
                raw = f.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                verb = line[:4].upper()
                if verb == "EHLO":
                    send("250-stub.smtp.test")
                    send("250 8BITMIME")
                elif verb == "HELO":
                    send("250 stub.smtp.test")
                elif verb == "MAIL":
                    mail_from, rcpts = line.split(":", 1)[1].strip(), []
                    send("250 OK")
                elif verb == "RCPT":
                    rcpts.append(line.split(":", 1)[1].strip().strip("<>"))
                    send("250 OK")
                elif verb == "DATA":
                    send("354 End data with <CR><LF>.<CR><LF>")
                    chunks = []
                    while True:
                        d = f.readline()
                        if not d or d in (b".\r\n", b".\n"):
                            break
                        if d.startswith(b".."):
                            d = d[1:]
                        chunks.append(d)
                    raw_msg = b"".join(chunks)
                    parsed = email.message_from_bytes(raw_msg, policy=policy.default)
                    html = ""
                    for part in parsed.walk():
                        if part.get_content_type() == "text/html":
                            html = part.get_content()
                    with self._lock:
                        self.messages.append({"from": mail_from, "rcpt": list(rcpts), "subject": str(parsed["Subject"]),
                                              "to": str(parsed["To"]), "html": html, "received_at": time.time()})
                    send("250 OK queued")
                elif verb in ("RSET", "NOOP"):
                    send("250 OK")
                elif verb == "QUIT":
                    send("221 Bye")
                    break
                else:
                    send("502 Command not implemented")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def for_rcpt(self, address: str) -> list[dict]:
        with self._lock:
            return [m for m in self.messages if address in m["rcpt"]]

    def config(self) -> dict:
        """Body for PUT /api/smtp-config pointing ODIN at this stub (plain SMTP, no TLS/AUTH)."""
        return {"enabled": True, "host": "127.0.0.1", "port": self.port, "username": "",
                "password": "", "from_address": "odin@stub.smtp.test", "use_tls": False}


# --------------------------------------------------------------------------------------------
# OIDC IdP (Entra-shaped)
# --------------------------------------------------------------------------------------------
class StubIdP:
    def __init__(self, client_id: str | None = None):
        self.port = _free_port()
        self.tenant = secrets.token_hex(8)
        self.base = f"http://127.0.0.1:{self.port}"
        self.issuer = f"{self.base}/{self.tenant}/v2.0"
        self.discovery_url = f"{self.issuer}/.well-known/openid-configuration"
        self.client_id = client_id or secrets.token_hex(8)
        self.client_secret = secrets.token_urlsafe(18)
        self.kid = secrets.token_hex(6)
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.requests: list[dict] = []
        self.codes: dict[str, dict] = {}  # code -> {"nonce","sub","email","name","extra_claims","userinfo"}
        self.tokens: dict[str, dict] = {}  # provider access token -> userinfo dict
        self._server = None

    # code issuance as the provider would do after the user authenticates
    def issue_code(self, nonce: str, sub: str, email_addr: str, name: str = "Synthetic Entra User",
                   userinfo: dict | None = None, extra_claims: dict | None = None) -> str:
        code = secrets.token_urlsafe(24)
        self.codes[code] = {"nonce": nonce, "sub": sub, "email": email_addr, "name": name,
                            "userinfo": userinfo, "extra_claims": extra_claims or {}}
        return code

    def _id_token(self, entry: dict) -> str:
        now = int(time.time())
        claims = {"iss": self.issuer, "aud": self.client_id, "sub": entry["sub"], "iat": now, "nbf": now,
                  "exp": now + 600, "nonce": entry["nonce"], "name": entry["name"],
                  "preferred_username": entry["email"], "tid": self.tenant, "ver": "2.0"}
        claims.update(entry["extra_claims"])
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self.kid})

    def jwks(self) -> dict:
        jwk = json.loads(RSAAlgorithm.to_jwk(self._key.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def discovery(self) -> dict:
        return {"issuer": self.issuer,
                "authorization_endpoint": f"{self.base}/{self.tenant}/oauth2/v2.0/authorize",
                "token_endpoint": f"{self.base}/{self.tenant}/oauth2/v2.0/token",
                "jwks_uri": f"{self.base}/{self.tenant}/discovery/v2.0/keys",
                "userinfo_endpoint": f"{self.base}/oidc/userinfo",
                "response_types_supported": ["code"], "id_token_signing_alg_values_supported": ["RS256"]}

    def start(self):
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = urlsplit(self.path).path
                idp.requests.append({"method": "GET", "path": path, "auth": self.headers.get("Authorization")})
                d = idp.discovery()
                if self.path == urlsplit(idp.discovery_url).path:
                    return self._json(200, d)
                if path == urlsplit(d["jwks_uri"]).path:
                    return self._json(200, idp.jwks())
                if path == urlsplit(d["userinfo_endpoint"]).path:
                    auth = self.headers.get("Authorization", "")
                    info = idp.tokens.get(auth[7:]) if auth.startswith("Bearer ") else None
                    if info is None:
                        return self._json(401, {"error": "invalid_token"})
                    return self._json(200, info)
                return self._json(404, {"error": "not_found"})

            def do_POST(self):
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                idp.requests.append({"method": "POST", "path": path, "form": form})
                if path != urlsplit(idp.discovery()["token_endpoint"]).path:
                    return self._json(404, {"error": "not_found"})
                entry = idp.codes.pop(form.get("code", ""), None)
                if (entry is None or form.get("grant_type") != "authorization_code"
                        or form.get("client_id") != idp.client_id or form.get("client_secret") != idp.client_secret):
                    return self._json(400, {"error": "invalid_grant"})
                access = secrets.token_urlsafe(24)
                idp.tokens[access] = entry["userinfo"] if entry["userinfo"] is not None else {
                    "sub": entry["sub"], "name": entry["name"], "mail": entry["email"],
                    "userPrincipalName": entry["email"]}
                return self._json(200, {"token_type": "Bearer", "expires_in": 3600, "access_token": access,
                                        "id_token": idp._id_token(entry), "scope": "openid profile email"})

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def token_requests(self) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST"]
