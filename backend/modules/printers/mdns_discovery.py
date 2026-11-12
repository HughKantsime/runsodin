"""One-shot mDNS browse for Moonraker (_moonraker._tcp.local), standard library only.

Sends one PTR query to 224.0.0.251:5353 from an ephemeral port (a "legacy unicast"
query, RFC 6762 section 6.7: responders answer straight back to this socket, so the
multicast group is never joined and port 5353 is never bound) and listens for a few
seconds. Everything received is untrusted LAN input and is parsed defensively:

* packets over MAX_PACKET bytes, or with more than MAX_RECORDS records, are refused;
* name compression pointers must point strictly backwards and are followed at most
  MAX_POINTER_JUMPS times (iteratively, never recursively), so a loop cannot spin;
* labels, names, record lengths and counts are bounds-checked before use;
* only class IN PTR, SRV and A records with a non-zero TTL are read.

This module only finds candidates. Whether a candidate may be offered or contacted is
decided by modules.printers.moonraker_discovery (the device destination policy).
"""
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import NamedTuple, Optional

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SERVICE = ("_moonraker", "_tcp", "local")
SCAN_SECONDS = 3.0

MAX_PACKET = 9000          # RFC 6762 section 17
MAX_DATAGRAMS = 128        # datagrams read per scan (accepted or not)
MAX_RECORDS = 128          # records per packet, all sections together
MAX_NAME_BYTES = 255
MAX_POINTER_JUMPS = 8
MAX_CANDIDATES = 256  # distinct endpoints heard per scan; the caller keeps the 16 most useful
MAX_ADDRESSES_PER_HOST = 4
MAX_DISPLAY = 64

TYPE_A, TYPE_PTR, TYPE_SRV = 1, 12, 33
ERROR_NO_MULTICAST = "no_multicast"


class MdnsParseError(ValueError):
    """A received packet is malformed, truncated, oversized or hostile."""


class Record(NamedTuple):
    kind: str      # "PTR" | "SRV" | "A"
    name: tuple    # owner name as a tuple of labels
    value: object  # PTR: tuple of labels; SRV: (port, tuple of labels); A: dotted quad


@dataclass
class ScanResult:
    packets: list = field(default_factory=list)   # [(sender_ip, [Record])]
    error: Optional[str] = None                   # ERROR_NO_MULTICAST when the socket is unusable
    malformed: int = 0


def build_query(service=SERVICE) -> bytes:
    """A PTR/IN question for `service`, class bit 15 clear (unicast reply to us)."""
    name = b"".join(bytes([len(label)]) + label.encode("ascii") for label in service) + b"\x00"
    return struct.pack("!6H", 0, 0, 1, 0, 0, 0) + name + struct.pack("!HH", TYPE_PTR, 1)


def _read_name(data: bytes, offset: int):
    """Decode the name at `offset`. Returns (labels, offset after the name's first form)."""
    labels = []
    total = 0
    jumps = 0
    end = None
    pos = offset
    while True:
        if pos >= len(data):
            raise MdnsParseError("truncated name")
        length = data[pos]
        if length == 0:
            pos += 1
            break
        kind = length & 0xC0
        if kind == 0xC0:
            if pos + 1 >= len(data):
                raise MdnsParseError("truncated compression pointer")
            target = ((length & 0x3F) << 8) | data[pos + 1]
            if end is None:
                end = pos + 2
            jumps += 1
            if jumps > MAX_POINTER_JUMPS:
                raise MdnsParseError("too many compression pointers")
            if target >= pos:
                # Strictly backwards: a pointer to itself or ahead can loop.
                raise MdnsParseError("compression pointer does not point backwards")
            pos = target
            continue
        if kind:
            raise MdnsParseError("reserved label type")
        pos += 1
        if pos + length > len(data):
            raise MdnsParseError("truncated label")
        total += length + 1
        if total > MAX_NAME_BYTES:
            raise MdnsParseError("name too long")
        labels.append(data[pos:pos + length].decode("utf-8", "replace"))
        pos += length
    return tuple(labels), (pos if end is None else end)


def _parse_rdata(data, rtype, rclass, ttl, owner, start, end):
    if ttl == 0 or (rclass & 0x7FFF) != 1:   # goodbye records and non-IN are ignored
        return None
    if rtype == TYPE_PTR:
        target, after = _read_name(data, start)
        if after > end:
            raise MdnsParseError("PTR name overruns its record")
        return Record("PTR", owner, target)
    if rtype == TYPE_SRV:
        if end - start < 7:
            raise MdnsParseError("short SRV record")
        _priority, _weight, port = struct.unpack_from("!HHH", data, start)
        target, after = _read_name(data, start + 6)
        if after > end:
            raise MdnsParseError("SRV name overruns its record")
        return Record("SRV", owner, (port, target))
    if rtype == TYPE_A:
        if end - start != 4:
            raise MdnsParseError("bad A record length")
        return Record("A", owner, socket.inet_ntoa(bytes(data[start:end])))
    return None


def parse_packet(data: bytes) -> list:
    """Parse one mDNS response into PTR/SRV/A records. Raises MdnsParseError."""
    if len(data) > MAX_PACKET:
        raise MdnsParseError("oversized packet")
    if len(data) < 12:
        raise MdnsParseError("truncated header")
    _tid, flags, qd, an, ns, ar = struct.unpack_from("!6H", data, 0)
    if not flags & 0x8000:
        raise MdnsParseError("not a response")
    if flags & 0x780F:
        raise MdnsParseError("non-standard opcode or error response")
    if qd + an + ns + ar > MAX_RECORDS:
        raise MdnsParseError("too many records")
    pos = 12
    for _ in range(qd):
        _name, pos = _read_name(data, pos)
        pos += 4
        if pos > len(data):
            raise MdnsParseError("truncated question")
    records = []
    for _ in range(an + ns + ar):
        owner, pos = _read_name(data, pos)
        if pos + 10 > len(data):
            raise MdnsParseError("truncated record header")
        rtype, rclass, ttl, rdlen = struct.unpack_from("!HHIH", data, pos)
        pos += 10
        end = pos + rdlen
        if end > len(data):
            raise MdnsParseError("truncated record data")
        record = _parse_rdata(data, rtype, rclass, ttl, owner, pos, end)
        if record is not None:
            records.append(record)
        pos = end
    return records


def scan(seconds: float = SCAN_SECONDS, *, socket_factory=socket.socket, clock=time.monotonic) -> ScanResult:
    """Send the query and collect parsed responses for `seconds`.

    An unusable socket (no multicast route, no permission) is reported as
    error=ERROR_NO_MULTICAST, not raised. A container on a bridge network usually
    sends fine and simply hears nothing.
    """
    result = ScanResult()
    try:
        sock = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        result.error = ERROR_NO_MULTICAST
        return result
    try:
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            sock.bind(("", 0))
            sock.sendto(build_query(), (MDNS_GROUP, MDNS_PORT))
        except OSError:
            result.error = ERROR_NO_MULTICAST
            return result
        deadline = clock() + seconds
        received = 0
        while received < MAX_DATAGRAMS:
            remaining = deadline - clock()
            if remaining <= 0:
                break
            try:
                sock.settimeout(remaining)
                data, address = sock.recvfrom(MAX_PACKET + 1)
            except (socket.timeout, OSError):
                break
            received += 1
            sender, port = address[0], address[1]
            if port != MDNS_PORT:
                continue   # mDNS responders answer from 5353
            try:
                result.packets.append((sender, parse_packet(data)))
            except MdnsParseError:
                result.malformed += 1
    finally:
        sock.close()
    return result


def _key(labels) -> tuple:
    return tuple(label.lower() for label in labels)


def _clean(text: str) -> str:
    """Printable characters only, bounded: names come from the LAN and are shown in the UI."""
    return "".join(ch for ch in text if ch.isprintable())[:MAX_DISPLAY].strip()


def build_candidates(packets) -> list:
    """Join PTR -> SRV -> A into [{name, host, ip, port, self_announced}].

    `packets` is ScanResult.packets. `self_announced` is True only when the SRV and the
    A record each came from the very address they announce, i.e. the printer speaking
    for itself. Only such a candidate may be probed, so a LAN host cannot point O.D.I.N.
    at a third machine or port.
    """
    instances, seen = [], set()
    srv, addresses = {}, {}
    for sender, records in packets:
        for record in records:
            if record.kind == "PTR" and _key(record.name) == _key(SERVICE):
                key = _key(record.value)
                if len(key) > len(SERVICE) and key[-len(SERVICE):] == _key(SERVICE) and key not in seen:
                    seen.add(key)
                    instances.append(record.value)
            elif record.kind == "SRV":
                port, target = record.value
                srv.setdefault(_key(record.name), (port, target, sender))
            elif record.kind == "A":
                found = addresses.setdefault(_key(record.name), [])
                if len(found) < MAX_ADDRESSES_PER_HOST and all(ip != record.value for ip, _ in found):
                    found.append((record.value, sender))
    # One candidate per endpoint: several service names for the same address and port would
    # otherwise use up the limit, and already-added printers could crowd out a new one before
    # the caller sorts them.
    candidates, seen = [], set()
    for instance in instances:
        entry = srv.get(_key(instance))
        if entry is None:
            continue
        port, target, srv_sender = entry
        if not 0 < port < 65536:
            continue
        host = _clean(".".join(target))
        name = _clean(".".join(instance[:-len(SERVICE)])) or host
        for ip, a_sender in addresses.get(_key(target), []):
            if (ip, port) in seen:
                continue
            seen.add((ip, port))
            candidates.append({
                "name": name, "host": host, "ip": ip, "port": port,
                "self_announced": ip == a_sender and ip == srv_sender,
            })
            if len(candidates) >= MAX_CANDIDATES:
                return candidates
    return candidates
