"""HTTP fetcher for the light scan (FR-LS-02, FR-LS-07, FR-SC-06, NFR-S-09).

- connect 10 s / read 20 s, body cut at 5 MB (streamed, aborted when exceeded);
- redirects followed manually (≤5 hops) with the egress guard on every hop;
- politeness: token bucket per host and per resolved IP before each request;
- records final URL, status, headers, TLS certificate summary, elapsed time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx

from payintel.crawl.egress import EgressBlocked, EgressGuard
from payintel.scheduler.politeness import RateLimiter, host_key, ip_key

MAX_HOPS = 5


@dataclass(frozen=True)
class TlsInfo:
    issuer: str
    subject: str
    not_before: str
    not_after: str
    san: tuple[str, ...]


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    elapsed_ms: int = 0
    tls: TlsInfo | None = None
    error: str | None = None
    hops: int = 0
    addresses: tuple[str, ...] = ()
    truncated: bool = False
    bytes_read: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and self.status < 400

    def text(self) -> str:
        return self.body.decode(_charset(self.headers), errors="replace")


def _charset(headers: dict[str, str]) -> str:
    ct = headers.get("content-type", "")
    if "charset=" in ct:
        cs = ct.split("charset=", 1)[1].split(";", 1)[0].strip().strip('"').lower()
        try:
            b"".decode(cs)
            return cs
        except LookupError:
            return "utf-8"
    return "utf-8"


def _tls_from(response: httpx.Response) -> TlsInfo | None:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    ssl_obj = stream.get_extra_info("ssl_object")
    if ssl_obj is None:
        return None
    try:
        cert: dict[str, Any] = ssl_obj.getpeercert() or {}
    except ValueError:
        return None
    if not cert:
        return None

    def flat(name: Any) -> str:
        return ", ".join("=".join(kv) for rdn in name for kv in rdn)

    san = tuple(v for k, v in cert.get("subjectAltName", ()) if k == "DNS")
    return TlsInfo(
        issuer=flat(cert.get("issuer", ())),
        subject=flat(cert.get("subject", ())),
        not_before=str(cert.get("notBefore", "")),
        not_after=str(cert.get("notAfter", "")),
        san=san,
    )


class Fetcher:
    def __init__(
        self,
        *,
        user_agent: str,
        guard: EgressGuard,
        limiter: RateLimiter,
        connect_timeout: float,
        read_timeout: float,
        max_bytes: int,
        host_rps: float,
        ip_rps: float,
        sleep: Any = None,
        verify_tls: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        import asyncio

        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout
            ),
            follow_redirects=False,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
            verify=verify_tls,
            limits=httpx.Limits(max_connections=400, max_keepalive_connections=50),
        )
        self._guard = guard
        self._limiter = limiter
        self._max_bytes = max_bytes
        self._host_rps = host_rps
        self._ip_rps = ip_rps
        self._sleep = sleep or asyncio.sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _polite_wait(self, hostname: str, addresses: tuple[str, ...]) -> None:
        wait = await self._limiter.acquire(host_key(hostname), self._host_rps)
        for ip in addresses[:1]:
            wait = max(
                wait, await self._limiter.acquire(ip_key(ip), self._ip_rps, burst=self._ip_rps)
            )
        if wait > 0:
            await self._sleep(wait)

    async def fetch(self, url: str, *, max_bytes: int | None = None) -> FetchResult:
        limit = max_bytes or self._max_bytes
        result = FetchResult(url=url, final_url=url, status=None)
        started = time.monotonic()
        current = url
        try:
            for hop in range(MAX_HOPS + 1):
                decision = await self._guard.check(current)
                result.addresses = decision.addresses
                await self._polite_wait(decision.hostname, decision.addresses)
                req = self._client.build_request("GET", current)
                response = await self._client.send(req, stream=True)
                try:
                    result.hops = hop
                    result.final_url = str(response.url)
                    result.status = response.status_code
                    result.headers = {k.lower(): v for k, v in response.headers.multi_items()}
                    if response.is_redirect and hop < MAX_HOPS:
                        location = response.headers.get("location")
                        if not location:
                            break
                        current = urljoin(current, location)
                        continue
                    result.tls = _tls_from(response)
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            result.truncated = True
                            chunks.append(chunk[: max(0, limit - (size - len(chunk)))])
                            break
                        chunks.append(chunk)
                    result.body = b"".join(chunks)
                    result.bytes_read = size
                    break
                finally:
                    await response.aclose()
            else:  # pragma: no cover - loop always breaks or continues
                result.error = "too_many_redirects"
            if result.hops >= MAX_HOPS and result.status is not None and 300 <= result.status < 400:
                result.error = "too_many_redirects"
        except EgressBlocked as exc:
            result.error = f"egress_blocked: {exc}"
        except httpx.ConnectTimeout:
            result.error = "connect_timeout"
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout):
            result.error = "read_timeout"
        except httpx.HTTPError as exc:
            result.error = f"http_error: {type(exc).__name__}"
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result
