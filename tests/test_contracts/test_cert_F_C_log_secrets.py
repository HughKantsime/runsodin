"""Log lines must not carry usable one-time login material (security re-review HIGH)."""
import pytest

from modules.system.sanitize import sanitize_log_line


@pytest.mark.parametrize("line,secret", [
    ('GET /api/auth/oidc/callback?code=IdPCode-123&state=st4te HTTP/1.1', "IdPCode-123"),
    ('GET /api/auth/oidc/callback?code=IdPCode-123&state=st4te HTTP/1.1', "st4te"),
    ('302 Location: /#oidc_code=OdinCode_xyz', "OdinCode_xyz"),
    ('GET /?oidc_code=LegacyCode HTTP/1.1', "LegacyCode"),
    ('GET /cb?foo=1&nonce=n0nce-v&x=2', "n0nce-v"),
    ('GET /cb?id_token=eyX.abc&refresh_token=r3fresh', "r3fresh"),
])
def test_url_login_parameters_are_redacted(line, secret):
    out = sanitize_log_line(line)
    assert secret not in out
    assert "[REDACTED]" in out


def test_parameter_names_and_ordinary_text_survive():
    out = sanitize_log_line("GET /api/auth/oidc/callback?code=abc&state=def HTTP/1.1 200")
    assert "code=[REDACTED]" in out and "state=[REDACTED]" in out and "HTTP/1.1 200" in out
    assert sanitize_log_line("zip code=12345 in prose") == "zip code=12345 in prose"
