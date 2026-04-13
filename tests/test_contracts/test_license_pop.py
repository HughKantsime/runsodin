"""
Contract + unit test — license proof-of-possession (S1).

Guards S1 from the 2026-04-12 Codex adversarial review: license
unactivation / reactivation used to trust the caller's knowledge of
`(key, installation_id)`. The full fix is a per-device Ed25519 keypair;
the client signs a server-issued challenge before the server accepts
any revocation-class operation.

This file tests the BACKEND's contribution: device keypair generation,
signature format, and canonical message format (which must match the
server-side `canonicalMessage()` in odin-site's license-pop.js).

Run without container: pytest tests/test_contracts/test_license_pop.py -v
"""

import ast
import base64
import os
import sys
import tempfile
from pathlib import Path

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
LICENSE_MGR = BACKEND_DIR / "license_manager.py"
ROUTES_HEALTH = BACKEND_DIR / "modules" / "system" / "routes_health.py"


class TestDeviceKeypairLifecycle:
    """get_device_keypair must be deterministic across calls on the same host."""

    def setup_method(self):
        # Use a fresh temp LICENSE_DIR per test so state is isolated.
        self._tmpdir = tempfile.mkdtemp(prefix="odin-license-test-")
        os.environ["LICENSE_DIR"] = self._tmpdir
        # Force reload so LICENSE_DIR env var takes effect
        if "license_manager" in sys.modules:
            del sys.modules["license_manager"]

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_generates_keypair_on_first_call(self):
        from license_manager import get_device_keypair, DEVICE_PRIVATE_KEY_FILENAME
        priv, pub_b64 = get_device_keypair()
        assert priv is not None
        assert isinstance(pub_b64, str)
        # Raw Ed25519 public key is 32 bytes → base64 is 44 chars (with padding).
        assert len(base64.b64decode(pub_b64)) == 32
        # The file must exist and be 0600.
        key_path = os.path.join(self._tmpdir, DEVICE_PRIVATE_KEY_FILENAME)
        assert os.path.isfile(key_path)
        mode = os.stat(key_path).st_mode & 0o777
        assert mode == 0o600, (
            f"Device private key at {key_path} has mode {oct(mode)}; "
            f"must be 0o600 so only the ODIN process user can read it."
        )

    def test_second_call_returns_same_pubkey(self):
        from license_manager import get_device_keypair
        _priv1, pub1 = get_device_keypair()
        _priv2, pub2 = get_device_keypair()
        assert pub1 == pub2, "Device pubkey must be stable — got different values on two calls."


class TestSignatureFormat:
    """sign_license_challenge must produce valid Ed25519 signatures over the
    canonical message format. The server-side format in odin-site's
    license-pop.js must match exactly."""

    def setup_method(self):
        self._tmpdir = tempfile.mkdtemp(prefix="odin-license-sig-test-")
        os.environ["LICENSE_DIR"] = self._tmpdir
        if "license_manager" in sys.modules:
            del sys.modules["license_manager"]

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_signature_verifies_with_server_format(self):
        """Replicate the server's canonical message, verify with the Python
        side's public key. Guards against protocol drift between client
        and server."""
        from license_manager import sign_license_challenge, get_device_keypair
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = "ODIN-TEST-KEY1-2345"
        installation_id = "inst-abcdef"
        nonce = base64.b64encode(b"\x01" * 32).decode()

        sig_b64 = sign_license_challenge("unactivate", key, installation_id, nonce)
        sig = base64.b64decode(sig_b64)
        # Ed25519 signatures are always 64 bytes.
        assert len(sig) == 64

        # Reconstruct the canonical message using the EXACT server format
        # (from api/_lib/license-pop.js canonicalMessage):
        #   `odin-license-v1\n${action}\n${key}\n${installation_id}\n${nonce}`
        msg = f"odin-license-v1\nunactivate\n{key}\n{installation_id}\n{nonce}".encode("utf-8")

        _priv, pub_b64 = get_device_keypair()
        raw_pub = base64.b64decode(pub_b64)
        pub = Ed25519PublicKey.from_public_bytes(raw_pub)
        # Will raise cryptography.exceptions.InvalidSignature on mismatch.
        pub.verify(sig, msg)

    def test_signature_rejects_wrong_message(self):
        """A signature over nonce A must NOT verify against nonce B."""
        from license_manager import sign_license_challenge, get_device_keypair
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature

        sig_b64 = sign_license_challenge("unactivate", "K", "I", "nonce-A")
        sig = base64.b64decode(sig_b64)

        wrong_msg = f"odin-license-v1\nunactivate\nK\nI\nnonce-B".encode("utf-8")
        _priv, pub_b64 = get_device_keypair()
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        with pytest.raises(InvalidSignature):
            pub.verify(sig, wrong_msg)

    def test_signature_rejects_unknown_action(self):
        from license_manager import sign_license_challenge
        with pytest.raises(ValueError, match="unknown action"):
            sign_license_challenge("delete-license", "K", "I", "N")


class TestRoutesWireKeypairAndSignature:
    """routes_health.py must send device_pubkey on activate and signed
    nonces on unactivate / reactivate. Source-level gate so this can't
    silently regress to the key+install-id-only form."""

    def test_activate_sends_device_pubkey(self):
        source = ROUTES_HEALTH.read_text()
        # The POST body to /api/v1/activate must include device_pubkey.
        assert '"device_pubkey": device_pubkey' in source, (
            "activate_license does not send device_pubkey to the license "
            "server. Without this, the S1 fix is not in place — the server "
            "has nothing to bind for later proof-of-possession checks."
        )

    def test_unactivate_fetches_challenge_and_signs(self):
        source = ROUTES_HEALTH.read_text()
        # Look for the unactivate handler region
        tree = ast.parse(source)
        fn_src = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "unactivate_license":
                fn_src = ast.get_source_segment(source, node)
                break
        assert fn_src, "unactivate_license missing"
        assert "_fetch_license_challenge" in fn_src, (
            "unactivate_license does not fetch a challenge before posting. "
            "S1 requires: nonce = fetch_challenge(); sig = sign(nonce); "
            "POST {key, install, nonce, sig}."
        )
        assert 'sign_license_challenge("unactivate"' in fn_src, (
            "unactivate_license does not sign the challenge with the "
            "'unactivate' action marker."
        )
        assert '"nonce": nonce' in fn_src and '"signature": signature' in fn_src, (
            "unactivate_license does not include nonce+signature in the "
            "outgoing POST body."
        )

    def test_reactivate_fetches_challenge_and_signs(self):
        source = ROUTES_HEALTH.read_text()
        tree = ast.parse(source)
        fn_src = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "reactivate_license":
                fn_src = ast.get_source_segment(source, node)
                break
        assert fn_src, "reactivate_license missing"
        assert "_fetch_license_challenge" in fn_src
        assert 'sign_license_challenge("reactivate"' in fn_src
        assert '"nonce": nonce' in fn_src and '"signature": signature' in fn_src


class TestOfflineActivationRequest:
    """The offline activation bundle must carry device_pubkey +
    bootstrap_signature so an attacker with just the key+install_id
    can't forge one."""

    def test_offline_activation_request_includes_pubkey_and_sig(self):
        source = ROUTES_HEALTH.read_text()
        tree = ast.parse(source)
        fn_src = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "build_activation_request":
                fn_src = ast.get_source_segment(source, node)
                break
        assert fn_src, (
            "build_activation_request handler missing — the offline path "
            "must be signed per S1 too."
        )
        assert "get_device_keypair()" in fn_src
        # multiline call — just look for both substrings
        assert "sign_license_challenge(" in fn_src
        assert '"activate-bootstrap"' in fn_src
        assert '"device_pubkey": device_pubkey' in fn_src
        assert '"bootstrap_signature": bootstrap_signature' in fn_src
