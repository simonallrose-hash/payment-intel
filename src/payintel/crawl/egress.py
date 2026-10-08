"""Egress guard against SSRF (NFR-S-09): private, link-local, metadata and reserved ranges.

Every HTTP hop (including each redirect target) is resolved first; a target
that resolves to a forbidden address, or to nothing, is refused before any
connection is opened. Checkout workers reuse the same guard. The resolver is a
callable so tests can inject fixed answers; the process-wide default uses the
system resolver (the light worker passes the Unbound-backed one).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from payintel.core.errors import GuardrailViolation

FORBIDDEN_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local incl. 169.254.169.254 metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:0:0/96"),  # v4-mapped: checked via the v4 rules below
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64
    ipaddress.ip_network("fc00::/7"),  # ULA
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("ff00::/8"),  # multicast
    ipaddress.ip_network("2001:db8::/32"),  # documentation
)
ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})


class EgressBlocked(GuardrailViolation):
    """Target refused by the egress policy (NFR-S-09)."""


def is_forbidden_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_multicast or addr.is_unspecified or addr.is_loopback or addr.is_link_local:
        return True
    return any(addr in net for net in FORBIDDEN_NETWORKS)


AddressResolver = Callable[[str], Awaitable[list[str]]]


async def system_resolve(hostname: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return sorted({str(info[4][0]) for info in infos})


@dataclass(frozen=True)
class EgressDecision:
    hostname: str
    addresses: tuple[str, ...]


class EgressGuard:
    def __init__(
        self,
        resolve: AddressResolver = system_resolve,
        *,
        allow_private: bool = False,
        allowed_ports: frozenset[int] = ALLOWED_PORTS,
    ) -> None:
        self._resolve = resolve
        self._allow_private = allow_private
        self._ports = allowed_ports

    async def check(self, url: str) -> EgressDecision:
        parts = urlsplit(url)
        if parts.scheme not in ALLOWED_SCHEMES:
            raise EgressBlocked(f"scheme {parts.scheme!r} not allowed", url=url)
        host = parts.hostname
        if not host:
            raise EgressBlocked("URL without host", url=url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in self._ports and not self._allow_private:
            raise EgressBlocked(f"port {port} not allowed", url=url)
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False
        if literal and not self._allow_private:
            raise EgressBlocked("IP literals are not scanned", url=url)
        addresses = [host] if literal else await self._resolve(host)
        if not addresses:
            raise EgressBlocked(f"{host} does not resolve", url=url)
        if not self._allow_private:
            bad = [ip for ip in addresses if is_forbidden_ip(ip)]
            if bad:
                raise EgressBlocked(f"{host} resolves to forbidden address {bad[0]}", url=url)
        return EgressDecision(hostname=host, addresses=tuple(addresses))
