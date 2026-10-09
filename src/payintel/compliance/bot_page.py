"""Content of the public bot page (FR-OO-01, LR-02, LR-06)."""

from __future__ import annotations

from dataclasses import dataclass

from payintel.core.settings import Settings


@dataclass(frozen=True)
class BotPage:
    user_agent: str
    ip_ranges: tuple[str, ...]
    contact_email: str
    company_domain: str
    max_rps_per_host: float
    respects_robots: bool
    never_orders: bool
    optout_methods: tuple[str, ...]


def build(settings: Settings) -> BotPage:
    return BotPage(
        user_agent=settings.identity.user_agent,
        ip_ranges=settings.identity.crawler_ip_ranges,
        contact_email=settings.identity.contact_email,
        company_domain=settings.identity.company_domain,
        max_rps_per_host=settings.scan.max_requests_per_second_per_host,
        respects_robots=True,
        never_orders=True,
        optout_methods=("dns_txt", "well_known"),
    )
