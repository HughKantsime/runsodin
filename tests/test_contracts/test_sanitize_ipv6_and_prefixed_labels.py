"""F16-16.9-03 regressions for modules.system.sanitize (support bundle / log viewer filter)."""
import secrets

import pytest

from modules.system.sanitize import sanitize_log_line


@pytest.mark.parametrize("line,secret", [
    ("broker [fd00:abcd:12::41]:8883 unreachable", "fd00:abcd:12::41"),
    ("peer 2001:0db8:85a3:0000:0000:8a2e:0370:7334 reset", "2001:0db8:85a3:0000:0000:8a2e:0370:7334"),
    ("link-local fe80::1%eth0 up", "fe80::1%eth0"),
    ("{'smtp_password': 'Synthetic-Value-1'}", "Synthetic-Value-1"),
    ("mqtt-api-key=Synthetic-Value-2", "Synthetic-Value-2"),
    ('"relay_token": "Synthetic-Value-3"', "Synthetic-Value-3"),
])
def test_redacts_ipv6_and_prefixed_secret_labels(line, secret):
    out = sanitize_log_line(line)
    assert secret not in out and "[REDACTED]" in out


@pytest.mark.parametrize("line", [
    "2026-10-14 10:00:01,123 INFO odin.api: started",
    "10:00:02.000 WRN go2rtc stream",
    "device aa:bb:cc:dd:ee:ff seen",
    "Retrying MQTT connect after transient error",
])
def test_times_macs_and_plain_text_are_kept(line):
    assert sanitize_log_line(line) == line


# ---------- review MEDIUM 5: remaining redaction gaps ----------
@pytest.mark.parametrize("template", [
    ("DATABASE_URL=postgresql://odin:{v}@db:5432/odin"),
    ("engine mysql+pymysql://reporter:{v}@mysql/odin"),
    ("broker mqtts://farm:{v}@broker.local:8883 down"),
    ("cache redis://:{v}@redis:6379/0"),
    ("queue amqp://guest:{v}@rabbit//"),
    ("AWS_SECRET_ACCESS_KEY={v}"),
    ("ODIN_SECRET={v}"),
    ("SPOOLMAN_API_KEY={v}"),
    ("ENCRYPTION_KEY: {v}"),
    ("DB_PASSWORD_FILE={v}"),
    ("MQTT_PASS={v}"),
    ("GITHUB_TOKEN={v}"),
    ("smtp login smtp_pass={v}"),
    ("camera pw={v} retry"),
    ("upstream replied 401 for Bearer {v}"),
    ("{'refresh_token': '{v}'}"),
])
def test_redacts_url_credentials_env_secrets_and_bearer_tokens(template):
    # The secret value is generated per run (no secret-shaped literals in the repo).
    secret = "Synthetic-" + secrets.token_hex(4) + ".x_y-z"
    out = sanitize_log_line(template.replace("{v}", secret))
    assert secret not in out and "[REDACTED]" in out


@pytest.mark.parametrize("line", [
    "std::vector<a::b> resolved",
    "parsed a::b and 1:: markers",
    "Foo::bar() returned",
    "bypass=1 compass=north keyboard=us",
    "sending 4 tokens of output",
])
def test_no_over_redaction_of_scope_operators_and_plain_words(line):
    assert sanitize_log_line(line) == line
