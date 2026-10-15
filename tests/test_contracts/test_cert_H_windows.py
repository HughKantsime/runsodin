"""Cert group H — F22-22.5-* Windows installer, certified at the non-Windows boundary.

No Windows host is available, so install.ps1 as a whole is not executed (kept as
HARDWARE_ACCEPTANCE items). What is executed here:
* the documented one-liner/manual URLs resolve to the files in this checkout (read-only GET),
* the compose volume rewrite install.ps1 performs (a .NET -replace with a regex that has
  identical semantics in Python re) applied to the real install/docker-compose.yml,
* install.ps1's own Windows->IANA time-zone conversion run in real PowerShell 7 / .NET 8
  (Microsoft's dotnet/sdk image, CERT_H_PWSH_IMAGE), with and without the .NET converter
  (the Windows PowerShell 5.1 path), and every resulting zone fed into the real image.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PS1 = ROOT / "install/install.ps1"
GUIDE = ROOT / "install/WINDOWS_INSTALL.md"
COMPOSE = ROOT / "install/docker-compose.yml"
IMAGE = os.environ.get("CERT_H_IMAGE", "")
RAW = "https://raw.githubusercontent.com/HughKantsime/runsodin/master"


def _ctx():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:  # pragma: no cover
        return ssl.create_default_context()


@pytest.mark.skipif(os.environ.get("CERT_H_NETWORK") != "1", reason="CERT_H_NETWORK=1 required")
@pytest.mark.parametrize("rel", ["install/install.ps1", "install/docker-compose.yml"])
def test_documented_raw_urls_serve_the_shipped_files(rel):
    assert f"{RAW}/{rel}" in GUIDE.read_text(encoding="utf-8")
    with urllib.request.urlopen(f"{RAW}/{rel}", timeout=30, context=_ctx()) as r:
        remote = r.read().decode("utf-8")
    assert remote.replace("\r\n", "\n") == (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n"), rel


def test_ps1_volume_rewrite_targets_windows_data_path_in_real_compose_file():
    src = PS1.read_text(encoding="utf-8")
    patterns = re.findall(r"-replace '([^']+)', \"\$InstallDir\\([^\"]+)\"", src)
    assert len(patterns) == 2, patterns
    compose = COMPOSE.read_text(encoding="utf-8")
    install_dir = r"C:\odin"
    for pattern, suffix in patterns:
        assert re.search(pattern, compose), pattern
        compose = re.sub(pattern, lambda _m, s=suffix: install_dir + "\\" + s, compose)
    assert r"- C:\odin\odin-data:/data" in compose
    assert r"- C:\odin\odin-data\go2rtc:/app/go2rtc" in compose
    assert "ODIN_DATA_PATH" not in compose


PWSH_IMAGE = os.environ.get("CERT_H_PWSH_IMAGE", "mcr.microsoft.com/dotnet/sdk:8.0-jammy")


def _ps1_block(begin: str, end: str) -> str:
    src = PS1.read_text(encoding="utf-8")
    return src[src.index(begin):src.index(end) + len(end)]


# Windows ids outside install.ps1's table: converted through .NET (PowerShell 7 path)
EXTRA_WINDOWS_IDS = ("Myanmar Standard Time", "SE Asia Standard Time", "Sri Lanka Standard Time", "Azores Standard Time",
                     "Cape Verde Standard Time", "Morocco Standard Time", "Jordan Standard Time", "Caucasus Standard Time",
                     "West Asia Standard Time", "N. Central Asia Standard Time", "Yakutsk Standard Time",
                     "Tonga Standard Time", "Fiji Standard Time", "Greenland Standard Time", "Paraguay Standard Time",
                     "Montevideo Standard Time", "Cuba Standard Time", "Haiti Standard Time", "Line Islands Standard Time")


def _run_converter() -> dict:
    """Run install.ps1's own ConvertTo-IanaTimeZone (and Write-Warn) in real PowerShell 7 / .NET 8."""
    warn = next(l for l in PS1.read_text(encoding="utf-8").splitlines() if l.startswith("function Write-Warn"))
    extra = ",".join(f"'{w}'" for w in EXTRA_WINDOWS_IDS)
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'", "$InstallDir = 'C:\\odin'", warn,
        _ps1_block("# BEGIN ConvertTo-IanaTimeZone", "# END ConvertTo-IanaTimeZone"),
        "$out = [ordered]@{ table = [ordered]@{}; net = [ordered]@{}; extra = [ordered]@{} }",
        "foreach ($k in ($WindowsToIanaZones.Keys | Sort-Object)) {",
        "  $out.table[$k] = ConvertTo-IanaTimeZone $k",
        "  $n = $null; [void][System.TimeZoneInfo]::TryConvertWindowsIdToIanaId($k, [ref]$n)",
        "  if ($n -and $IanaLegacyAliases.ContainsKey($n)) { $n = $IanaLegacyAliases[$n] }",
        "  $out.net[$k] = $n }",
        f"foreach ($k in @({extra})) {{ $out.extra[$k] = ConvertTo-IanaTimeZone $k 6>$null }}",
        "$out.passthrough = ConvertTo-IanaTimeZone 'America/Chicago'",
        "$out.unknown = ConvertTo-IanaTimeZone 'Mars Standard Time' 6>&1 | Out-String",
        "$out.unknown_value = ConvertTo-IanaTimeZone 'Mars Standard Time' 6>$null",
        "$out.ps51_extra = ConvertTo-IanaTimeZone 'Myanmar Standard Time' -NoDotNet 6>$null",
        "$out.ps51_eastern = ConvertTo-IanaTimeZone 'Eastern Standard Time' -NoDotNet",
        "$out | ConvertTo-Json -Depth 4",
    ])
    result = subprocess.run(["docker", "run", "--rm", "--network", "none", "-e", "TERM=dumb", PWSH_IMAGE, "pwsh",
                             "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
                            timeout=300)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout)


def _offsets(zones: set[str]) -> dict[str, tuple[str, str, bool]]:
    """(January offset, July offset, zoneinfo file present) for each zone, in the real ODIN image."""
    loop = ("for z in \"$@\"; do printf '%s|%s|%s|%s\\n' \"$z\" "
            "\"$(TZ=$z date -d 2026-01-15T12:00:00Z +%z)\" \"$(TZ=$z date -d 2026-07-15T12:00:00Z +%z)\" "
            "\"$([ -f /usr/share/zoneinfo/$z ] && echo y || echo n)\"; done")
    result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "sh", IMAGE, "-c", loop,
                             "sh", *sorted(zones)], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    rows = [line.split("|") for line in result.stdout.splitlines()]
    return {z: (jan, jul, present == "y") for z, jan, jul, present in rows}


@pytest.mark.skipif(not IMAGE or shutil.which("docker") is None, reason="CERT_H_IMAGE and docker required")
def test_windows_timezone_value_written_by_installer_is_honoured_by_container():
    converted = _run_converter()
    table, net, extra = converted["table"], converted["net"], converted["extra"]
    assert len(table) >= 50 and set(table) == set(net)
    assert table["Eastern Standard Time"] == converted["ps51_eastern"] == "America/New_York"
    assert converted["passthrough"] == "America/Chicago"
    assert converted["unknown_value"].strip() == "UTC" and converted["ps51_extra"].strip() == "UTC"
    assert "Unknown Windows time zone 'Mars Standard Time'" in converted["unknown"]
    assert "will run in UTC" in converted["unknown"]
    offsets = _offsets(set(table.values()) | set(net.values()) | set(extra.values()) | {"UTC"})
    for windows_id in table:
        mapped, dotnet = table[windows_id], net[windows_id]
        # every value install.ps1 can write is a zone the container's tzdata really has ...
        assert offsets[mapped][2], (windows_id, mapped)
        # ... and the table agrees with .NET/ICU (CLDR) on the actual offsets
        assert offsets[dotnet][2] and offsets[mapped][:2] == offsets[dotnet][:2], (windows_id, mapped, dotnet)
        if windows_id not in ("UTC", "Greenwich Standard Time"):
            assert offsets[mapped][:2] != offsets["UTC"][:2], windows_id  # not a silent UTC fallback
    for windows_id, mapped in extra.items():
        assert mapped != "UTC" and offsets[mapped][2], (windows_id, mapped)
    assert offsets["America/New_York"][:2] == ("-0500", "-0400")
    # the Windows id itself is what the old installer wrote; the container silently ran in UTC
    raw = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "date", "-e",
                          "TZ=Eastern Standard Time", IMAGE, "-d", "2026-01-15T12:00:00Z", "+%z"],
                         capture_output=True, text=True, timeout=120).stdout.strip()
    assert raw == "+0000"
    src = PS1.read_text(encoding="utf-8")
    # the value written to .env (TZ=$tz) is the converted one
    assert "$tz = ConvertTo-IanaTimeZone $windowsTz" in src and "\nTZ=$tz\n" in src
    assert src.count("(Get-TimeZone).Id") == 1 and "$windowsTz = (Get-TimeZone).Id" in src


def test_guide_has_prerequisites_manual_steps_update_firewall_uninstall_troubleshooting():
    text = GUIDE.read_text(encoding="utf-8")
    headings = set(re.findall(r"^#{2,3} (.+)$", text, flags=re.M))
    for section in ("Prerequisites", "Option A: One-command installer (recommended)", "Option B: Manual install",
                    "Updating", "Firewall Configuration", "Uninstalling", "Troubleshooting"):
        assert section in headings, section
    assert "irm https://raw.githubusercontent.com/HughKantsime/runsodin/master/install/install.ps1 | iex" in text
