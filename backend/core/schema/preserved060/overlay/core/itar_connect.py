"""ITAR mode for connections that are not HTTP: mail (smtplib) and MQTT (paho).

The rule is the same as for HTTP (core.itar.pin_for_request): look the host name up ONCE,
refuse unless every address is on the local network, and connect to one of those addresses.
Checking the name and then handing the name to the library is not enough: the library looks
it up again when it connects (and paho again on every reconnect), and a name that answered
with a local address for the check can answer with a public one for the connection.

Both clients keep the host NAME for TLS: the server name sent, and the name a certificate is
matched against, are unchanged. Only the address the socket connects to is fixed.

With ITAR mode off both functions return the plain library client and nothing is looked up
here; behaviour is identical to calling the library directly.
"""
from __future__ import annotations

import ipaddress
import smtplib
import socket

from core.itar import ItarOutboundBlocked, _is_local_address, is_itar_mode


def local_network_addresses(host: str, port: int, scheme: str = "net") -> list[str]:
    """The addresses `host` has, from one lookup, when every one is on the local network.

    Raises ItarOutboundBlocked for an empty host, a name that does not resolve, or an answer
    with any address outside the local network (a mixed answer is refused whole). An IP
    literal is checked without a lookup. Callers connect to the returned addresses and never
    look the name up again.
    """
    name = str(host or "").strip()
    if not name:
        raise ItarOutboundBlocked(f"ODIN_ITAR_MODE=1 refused {scheme} connect: no host")
    try:
        literal = ipaddress.ip_address(name.strip("[]").split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not _is_local_address(literal):
            raise ItarOutboundBlocked(
                f"ODIN_ITAR_MODE=1 refused {scheme} connect — {name!r} is a public address.")
        return [str(literal)]
    try:
        infos = socket.getaddrinfo(name, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise ItarOutboundBlocked(
            f"ODIN_ITAR_MODE=1 refused {scheme} connect — {name!r} could not be resolved.") from exc
    addresses: list[str] = []
    for info in infos:
        text = str(info[4][0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            address = None
        if address is None or not _is_local_address(address):
            raise ItarOutboundBlocked(
                f"ODIN_ITAR_MODE=1 refused {scheme} connect — "
                f"{name!r} resolves to a public address.")
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise ItarOutboundBlocked(
            f"ODIN_ITAR_MODE=1 refused {scheme} connect — {name!r} returned no addresses.")
    return addresses


def _connect_to_one(addresses, port, timeout, source_address=None):
    """A TCP socket to the first of `addresses` that answers. Only IP literals reach
    socket.create_connection here, so nothing is looked up."""
    last_error = None
    for address in addresses:
        try:
            if source_address:
                return socket.create_connection((address, port), timeout, source_address)
            return socket.create_connection((address, port), timeout)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("no address to connect to")


class _PinnedSMTP(smtplib.SMTP):
    """smtplib.SMTP whose socket goes to the checked addresses.

    smtplib keeps the host name in `self._host` and passes it to starttls() as the TLS
    server name, so the name on the certificate is still the configured host.
    """

    def __init__(self, host, port, addresses, **kwargs):
        self._odin_pinned = (str(host), int(port), list(addresses))
        super().__init__(host, port, **kwargs)

    def _get_socket(self, host, port, timeout):
        pinned_host, pinned_port, addresses = self._odin_pinned
        if host != pinned_host or int(port) != pinned_port:
            raise ItarOutboundBlocked(
                "ODIN_ITAR_MODE=1 refused smtp connect: not the destination that was checked")
        if timeout is not None and not timeout:
            raise ValueError("Non-blocking socket (timeout=0) is not supported")
        return _connect_to_one(addresses, pinned_port, timeout, self.source_address)


def smtp_connection(host, port, **kwargs) -> smtplib.SMTP:
    """smtplib.SMTP(host, port, **kwargs), connected.

    Under ITAR mode the host is looked up once, refused unless local, and the connection is
    made to that address (ItarOutboundBlocked otherwise, before any socket is opened).
    """
    if not is_itar_mode():
        return smtplib.SMTP(host, port, **kwargs)
    port = int(port)
    return _PinnedSMTP(host, port, local_network_addresses(host, port, "smtp"), **kwargs)


def mqtt_client(host, port, *args, **kwargs):
    """paho.mqtt.client.Client(*args, **kwargs) for a broker at host:port.

    The caller still calls client.connect(host, port, ...) with the same host and port.
    Under ITAR mode the host is looked up once here, refused unless local
    (ItarOutboundBlocked), and every socket this client opens (the first connection and
    every reconnect, automatic or not) goes to that address. A connect() to any other host
    or port on this client is refused. Environment and configured proxies are not used.
    """
    import paho.mqtt.client as mqtt

    if not is_itar_mode():
        return mqtt.Client(*args, **kwargs)
    port = int(port)
    addresses = local_network_addresses(host, port, "mqtt")
    if not hasattr(mqtt.Client, "_create_socket_connection"):
        # The hook this relies on is gone (a paho upgrade): refuse, never fall back to the name.
        raise ItarOutboundBlocked("ODIN_ITAR_MODE=1 refused mqtt connect: this paho version cannot be pinned")

    class PinnedClient(mqtt.Client):
        _odin_pinned = (str(host), port, addresses)

        def _create_socket_connection(self):
            pinned_host, pinned_port, pinned_addresses = self._odin_pinned
            if self._host != pinned_host or int(self._port) != pinned_port:
                raise ItarOutboundBlocked(
                    "ODIN_ITAR_MODE=1 refused mqtt connect: not the destination that was checked")
            source = (self._bind_address, self._bind_port) if self._bind_address or self._bind_port else None
            return _connect_to_one(pinned_addresses, pinned_port, self._connect_timeout, source)

    return PinnedClient(*args, **kwargs)
