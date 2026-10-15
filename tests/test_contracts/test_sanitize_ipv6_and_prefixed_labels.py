"""F16-16.9-03 regressions for modules.system.sanitize (support bundle / log viewer filter)."""
import pytest

from modules.system.sanitize import sanitize_log_line


@pytest.mark.parametrize("line,secret", [
    ("broker [fd00:abcd:12::41]:8883 unreachable", "fd00:abcd:12::41"),
    ("peer 2001:0db8:85a3:0000:0000:8a2e:0370:7334 reset", "2001:0db8:85a3:0000:0000:8a2e:0370:7334"),
    ("link-local fe80::1%eth0 up", "fe80::1%eth0"),
    ("bound to [::1]:8000", "::1"),
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
