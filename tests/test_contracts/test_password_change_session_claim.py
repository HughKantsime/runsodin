"""F12-12.1-07: the session-revocation claim says exactly what a password change does.

Behaviour (tests/test_contracts/test_cert_F_C_auth_security.py): changing your own
password keeps the calling session and revokes the rest; an admin changing another
user's password, and a reset link, revoke every session of that user."""
from pathlib import Path

FEATURES = (Path(__file__).resolve().parents[2] / "FEATURES.md").read_text(encoding="utf-8")
CLAIM = ("- Password change revokes all other sessions: changing your own password keeps the session you "
         "changed it from; an admin changing another user's password, or a reset link, revokes all of that "
         "user's sessions (v1.3.66)")


def test_password_change_claim_matches_the_revocation_behaviour():
    assert CLAIM in FEATURES
    assert "- Password change revokes all existing sessions" not in FEATURES
