"""Camera-stack review LOW 7: go2rtc's API port 1984 is never published; WebRTC 8555 is."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _published(ports):
    return [str(p).split("#")[0].strip().strip('"') for p in ports or []]


def test_enterprise_compose_keeps_go2rtc_api_on_the_internal_network():
    go2rtc = yaml.safe_load((ROOT / "docker-compose.enterprise.yml").read_text())["services"]["go2rtc"]
    published = _published(go2rtc.get("ports"))
    assert "8555:8555" in published
    assert not any(p.endswith(":1984") or p == "1984" for p in published), published
    assert [str(p) for p in go2rtc.get("expose", [])] == ["1984"]


def test_install_compose_does_not_publish_go2rtc_api():
    odin = yaml.safe_load((ROOT / "install" / "docker-compose.yml").read_text())["services"]["odin"]
    published = _published(odin.get("ports"))
    assert "${ODIN_WEBRTC_PORT:-8555}:8555" in published and "${ODIN_HTTP_PORT:-8000}:8000" in published
    assert not any(p.endswith(":1984") for p in published), published


def test_windows_install_guide_has_no_go2rtc_firewall_rule():
    guide = (ROOT / "install" / "WINDOWS_INSTALL.md").read_text()
    assert "localport=1984" not in guide and "localport=8555" in guide
