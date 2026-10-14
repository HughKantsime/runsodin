"""Import-safe smart-plug HTTP protocols, shared by manual and owned workers.

No database, crypto, timer or publisher import. ITAR is checked at call time.
"""
import json
import logging
from typing import Optional, Tuple

log = logging.getLogger("smart_plug")


def http_request(url: str, method: str = "GET", headers: dict = None,
                  data: str = None, timeout: int = 5) -> Tuple[int, str]:
    """Simple HTTP request using urllib (no external deps).

    Direct configured destination only: no redirects or environment proxies.
    Runtime ITAR policy applies before opening, but is not DNS pinning.
    """
    try:
        from core.itar import enforce_request_destination, ItarOutboundBlocked
        enforce_request_destination(url)
    except ItarOutboundBlocked as exc:
        log.warning("Smart plug destination blocked by local policy")
        return 0, "Destination blocked by local policy"
    except Exception:
        return 0, "Destination policy could not be verified"

    import urllib.request
    import urllib.error

    try:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        req = urllib.request.Request(url, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        if data:
            req.data = data.encode("utf-8")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(65537)
            if len(raw) > 65536:
                return 0, "HTTP response exceeds limit"
            body = raw.decode("utf-8")
            return resp.status, body
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, "HTTP request was rejected"
    except Exception as e:
        log.error("Smart plug HTTP request failed")
        return 0, "HTTP request failed"


def tasmota_power(host: str, action: str = "TOGGLE", *, request=None) -> Optional[bool]:
    """
    Control Tasmota smart plug via HTTP.
    action: "ON", "OFF", "TOGGLE", or "" (status query)
    Returns True=on, False=off, None=error
    """
    request = request or http_request
    url = f"http://{host}/cm?cmnd=Power%20{action}"
    status, body = request(url)
    if status == 200:
        try:
            data = json.loads(body)
            power = data.get("POWER", data.get("Power", ""))
            return {"ON": True, "OFF": False}.get(power) if isinstance(power, str) else None
        except Exception as e:
            log.debug("Failed to parse Tasmota power response")
    return None


def ha_switch(ha_url: str, entity_id: str, token: str, action: str = "toggle", *, request=None, readback=None) -> Optional[bool]:
    """
    Control a Home Assistant switch/plug entity.
    action: "turn_on", "turn_off", "toggle"
    Returns True=on, False=off, None=error
    """
    request = request or http_request
    readback = readback or (lambda url, entity, auth: ha_get_state(url, entity, auth, request=request))
    service = f"switch/{action}"
    url = f"{ha_url.rstrip('/')}/api/services/{service}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = json.dumps({"entity_id": entity_id})
    status, body = request(url, method="POST", headers=headers, data=payload)
    
    if status == 200:
        # Query state after action
        return readback(ha_url, entity_id, token)
    return None


def ha_get_state(ha_url: str, entity_id: str, token: str, *, request=None) -> Optional[bool]:
    """Get current state of a Home Assistant entity."""
    request = request or http_request
    url = f"{ha_url.rstrip('/')}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {token}"}
    status, body = request(url, headers=headers)
    if status == 200:
        try:
            data = json.loads(body)
            state = data.get("state")
            return {"on": True, "off": False}.get(state) if isinstance(state, str) else None
        except Exception as e:
            log.debug("Failed to parse Home Assistant state response")
    return None


