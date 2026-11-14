"""Klipper/Moonraker network discovery: policy, optional confirmation, configured marking.

mdns_discovery finds candidates; this module decides what may be offered and reports it.

* Every candidate must be an IPv4 address on the local network (RFC 1918) that the
  device destination policy accepts (core.webhook_utils.device_destination_ips: not
  loopback, link-local, multicast, metadata, reserved, nor one of O.D.I.N.'s own
  services). mDNS never leaves the LAN, so a public address is never offered, in or
  out of ITAR mode (core.itar.is_private_destination is the same test ITAR applies).
* A candidate that announced itself is confirmed with one pinned GET /printer/info
  (2 s timeout, no credentials, no redirects, no proxy) for klippy state and hostname.
* Hosts already configured as printers (within the caller's organisation scope) are marked.
"""
import ipaddress
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import or_

from core.itar import is_private_destination
from core.rbac import get_org_scope, should_filter_org
from core.webhook_utils import (
    DeviceDestinationBlocked, device_destination_ips, is_blocked_device_address,
    normalize_hostname, split_device_host,
)
from modules.printers import mdns_discovery
from modules.printers.models import Printer

REASON_NO_MULTICAST = "Discovery needs the container on the host network or mDNS reflection."
REASON_NONE_HEARD = ("No Klipper printers answered. Moonraker only announces itself when its [zeroconf] "
                     "section is enabled in moonraker.conf. If O.D.I.N. runs in a container, discovery also "
                     "needs the container on the host network or mDNS reflection. You can always type the address.")
REASON_NOT_LOCAL = "Printers answered, but none had a local network address that O.D.I.N. may connect to."
REASON_BUSY = "A discovery scan is already running; try again in a few seconds."

PROBE_TIMEOUT_SECONDS = 2
PROBE_WORKERS = 8
MAX_OFFERED = 16          # results shown per scan (not-yet-added printers first)
MAX_INFO_BYTES = 65536

_scan_lock = threading.Lock()


def allowed_by_policy(ip: str, port: int) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if address.version != 4 or is_blocked_device_address(address):
        return False
    if not is_private_destination(ip):
        return False
    try:
        device_destination_ips(ip, port)
    except DeviceDestinationBlocked:
        return False
    return True


def offered_candidates(candidates) -> tuple:
    """(candidates the policy accepts, number refused)."""
    kept = [c for c in candidates if allowed_by_policy(c["ip"], c["port"])]
    return kept, len(candidates) - len(kept)


def _bounded(value, limit: int):
    if not isinstance(value, str):
        return None
    return "".join(ch for ch in value if ch.isprintable())[:limit].strip() or None


MAX_HEADER_BYTES = 8192


def _read_info(ip: str, port: int, deadline: float):
    """Body of GET /printer/info over one plain socket, or None.

    A raw HTTP/1.0 exchange, so ONE deadline covers connect, headers and body (an HTTP
    client's timeout is per read: a host that trickles bytes keeps it alive for ever), and
    nothing is ever decompressed (a small compressed answer can expand to megabytes).
    Every byte is counted before it is kept.
    """
    import socket
    import time

    def left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("probe deadline")
        return remaining

    request = (f"GET /printer/info HTTP/1.0\r\nHost: {ip}:{port}\r\nAccept: application/json\r\n"
               "Accept-Encoding: identity\r\nConnection: close\r\n\r\n").encode("ascii")
    data = bytearray()
    sock = socket.create_connection((ip, port), timeout=left())
    try:
        sock.settimeout(left())
        sock.sendall(request)
        while True:
            sock.settimeout(left())
            chunk = sock.recv(8192)
            if not chunk:
                break
            if len(data) + len(chunk) > MAX_HEADER_BYTES + MAX_INFO_BYTES:
                return None
            data += chunk
            head, found, body = bytes(data).partition(b"\r\n\r\n")
            if len(head) > MAX_HEADER_BYTES:
                return None                      # with or without the end of the headers in sight
            if found:
                length = _header(head, b"content-length")
                if length is not None and length.isdigit() and len(body) >= int(length):
                    break
    finally:
        sock.close()
    head, found, body = bytes(data).partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].split()
    if not found or len(head) > MAX_HEADER_BYTES or len(status) < 2 or not status[0].startswith(b"HTTP/1.") or status[1] != b"200":
        return None
    if (_header(head, b"content-encoding") or b"identity").lower() != b"identity" or _header(head, b"transfer-encoding"):
        return None
    return body if len(body) <= MAX_INFO_BYTES else None


def _header(head: bytes, name: bytes):
    for line in head.split(b"\r\n")[1:]:
        key, _, value = line.partition(b":")
        if key.strip().lower() == name:
            return value.strip()
    return None


def probe_klippy(ip: str, port: int):
    """GET /printer/info, policy-checked, no credentials. Dict on success, else None."""
    import json
    import time
    from core.webhook_utils import pinned_device_destination
    try:
        with pinned_device_destination(f"http://{ip}:{port}/printer/info"):
            body = _read_info(ip, port, time.monotonic() + PROBE_TIMEOUT_SECONDS)
        result = json.loads(body).get("result") if body is not None else None
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    state = _bounded(result.get("state"), 32)
    if state is None:
        return None
    return {"klippy_state": state, "hostname": _bounded(result.get("hostname"), 64)}


def configured_hosts(db, current_user) -> set:
    """Normalised hosts of the printers this caller can see."""
    query = db.query(Printer.api_host).filter(Printer.api_host.isnot(None))
    scope = get_org_scope(current_user)
    if should_filter_org(current_user, scope):
        query = query.filter(or_(Printer.org_id == scope, Printer.org_id.is_(None), Printer.shared.is_(True)))
    hosts = set()
    for (value,) in query.all():
        try:
            host, _port = split_device_host(value)
            hosts.add(normalize_hostname(host))
        except (DeviceDestinationBlocked, UnicodeError):
            continue
    return hosts


def _is_configured(candidate: dict, configured: set) -> bool:
    host = candidate["host"]
    names = {candidate["ip"], host, host[:-len(".local")] if host.lower().endswith(".local") else host}
    for name in names:
        try:
            if name and normalize_hostname(name) in configured:
                return True
        except Exception:  # a hostile or malformed announced name must never fail the whole scan
            continue
    return False


def _result(printers, reason, filtered_out):
    return {"printers": printers, "reason": reason, "filtered_out": filtered_out}


def discover(db, current_user, *, scan=None, probe=None) -> dict:
    """Run one scan and return {"printers": [...], "reason": str|None, "filtered_out": int}."""
    scan = scan or mdns_discovery.scan
    probe = probe or probe_klippy
    if not _scan_lock.acquire(blocking=False):
        return _result([], REASON_BUSY, 0)
    try:
        scanned = scan()
    finally:
        _scan_lock.release()
    if scanned.error:
        return _result([], REASON_NO_MULTICAST, 0)
    heard = mdns_discovery.build_candidates(scanned.packets)
    offered, filtered_out = offered_candidates(heard)
    if not offered:
        return _result([], REASON_NONE_HEARD if not heard else REASON_NOT_LOCAL, filtered_out)

    configured = configured_hosts(db, current_user)
    for candidate in offered:
        candidate["already_configured"] = _is_configured(candidate, configured)
    # Printers not added yet come first and are the ones kept when more answer than are shown,
    # so a large farm can still find its remaining printers.
    offered = sorted(offered, key=lambda c: c["already_configured"])[:MAX_OFFERED]
    targets = [c for c in offered if c["self_announced"] and not c["already_configured"]]
    if targets:
        with ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(targets))) as pool:
            infos = dict(zip((id(c) for c in targets), pool.map(lambda c: probe(c["ip"], c["port"]), targets)))
    else:
        infos = {}
    printers = []
    for candidate in offered:
        attempted = candidate["self_announced"] and not candidate["already_configured"]
        info = infos.get(id(candidate)) or {}
        printers.append({
            "name": candidate["name"],
            "host": candidate["host"],
            "ip": candidate["ip"],
            "port": candidate["port"],
            "address": candidate["ip"] if candidate["port"] == 80 else f"{candidate['ip']}:{candidate['port']}",
            "already_configured": candidate["already_configured"],
            "confirmed": bool(info) if attempted else None,
            "klippy_state": info.get("klippy_state"),
            "hostname": info.get("hostname"),
        })
    return _result(printers, None, filtered_out)
