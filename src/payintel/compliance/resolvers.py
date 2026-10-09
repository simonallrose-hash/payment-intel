"""Real-world proof lookups for opt-out verification (FR-OO-02).

Both callables are injected into `AppState` so tests swap them for fakes.
HTTP fetches go through the same address policy as the crawler: the host is
resolved first and private, loopback or link-local answers are refused
(NFR-S-09), redirects are not followed, and at most 4 KiB is read.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

import dns.exception
import dns.resolver
import httpx

from payintel.core.errors import ValidationError
from payintel.core.settings import Settings
from payintel.crawl.egress import is_forbidden_ip

MAX_BODY = 4096


def dns_txt_lookup(settings: Settings) -> Callable[[str], list[str]]:
    resolver = dns.resolver.Resolver(configure=not settings.discovery.dns_nameservers)
    if settings.discovery.dns_nameservers:
        resolver.nameservers = list(settings.discovery.dns_nameservers)
        resolver.port = settings.discovery.dns_port
    resolver.lifetime = settings.discovery.dns_timeout_seconds

    def lookup(domain: str) -> list[str]:
        try:
            answer = resolver.resolve(domain, "TXT")
        except (dns.exception.DNSException, OSError):
            return []
        out: list[str] = []
        for rr in answer:
            strings = getattr(rr, "strings", ())
            out.append(b"".join(strings).decode("utf-8", errors="replace"))
        return out

    return lookup


def _public_addresses(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return sorted({str(info[4][0]) for info in infos})


def http_get(url: str, *, timeout: float = 10.0) -> tuple[int, str]:
    """GET a public https URL without redirects; `(status, first 4 KiB of text)`."""
    parts = httpx.URL(url)
    if parts.scheme != "https" or not parts.host:
        raise ValidationError("only https URLs are fetched", url=url)
    addresses = _public_addresses(parts.host)
    if not addresses or any(is_forbidden_ip(ip) for ip in addresses):
        return 0, ""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            with client.stream("GET", url, headers={"User-Agent": "PayIntel-optout/1.0"}) as r:
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) >= MAX_BODY:
                        break
                return r.status_code, body[:MAX_BODY].decode("utf-8", errors="replace")
    except httpx.HTTPError:
        return 0, ""
