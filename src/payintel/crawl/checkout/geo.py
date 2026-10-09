"""Geo parameter of a checkout walk (FR-CW-11).

The walk runs with the language, `Accept-Language` and synthetic address of
the store's country (the identity, FR-CW-05); that country is recorded as
`checkout_country`. When the store serves a country other than the one our
egress addresses are in, and the operator configured a proxy for that
country, the browser context goes through it *for geolocation only*: same
identifying User-Agent, same robots/politeness rules, chosen once before the
walk and never as a reaction to a block (LR-03). Without a proxy for the
country the walk still runs with the country's language and address from
our own addresses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

REASON_EGRESS = "egress_country"
REASON_PROXY = "geolocation_proxy"
REASON_NO_PROXY = "no_proxy_for_country"


@dataclass(frozen=True)
class ProxyConfig:
    server: str  # scheme://host:port, no credentials
    username: str | None = None
    password: str | None = None

    @classmethod
    def parse(cls, url: str) -> ProxyConfig:
        u = urlsplit(url.strip())
        if not u.hostname or u.scheme not in ("http", "https", "socks5"):
            raise ValueError("proxy must be http(s)://[user:pass@]host:port or socks5://host:port")
        server = f"{u.scheme}://{u.hostname}" + (f":{u.port}" if u.port else "")
        return cls(server=server, username=u.username or None, password=u.password or None)

    def as_playwright(self) -> dict[str, str]:
        out = {"server": self.server}
        if self.username:
            out["username"] = self.username
        if self.password:
            out["password"] = self.password
        return out

    @property
    def redacted(self) -> str:
        """What goes into journals and manifests: never the credentials."""
        return self.server


@dataclass(frozen=True)
class GeoChoice:
    country: str
    proxy: ProxyConfig | None
    reason: str

    def as_log(self) -> dict[str, str | None]:
        return {
            "country": self.country,
            "proxy": self.proxy.redacted if self.proxy else None,
            "reason": self.reason,
        }


def choose(country: str, *, egress_country: str, proxies: Mapping[str, str]) -> GeoChoice:
    """Decide once, before the walk, whether a geolocation proxy applies."""
    cc = country.upper()
    if cc == egress_country.upper():
        return GeoChoice(cc, None, REASON_EGRESS)
    url = proxies.get(cc) or proxies.get(cc.lower())
    if not url:
        return GeoChoice(cc, None, REASON_NO_PROXY)
    return GeoChoice(cc, ProxyConfig.parse(url), REASON_PROXY)
