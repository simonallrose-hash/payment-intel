"""NFR-S-09 egress guard, FR-LS-04 PII sanitiser, FR-LS-01 robots semantics."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from payintel.crawl.egress import EgressBlocked, EgressGuard, is_forbidden_ip
from payintel.crawl.light.robots import agent_token, parse_robots
from payintel.crawl.sanitize import EMAIL_MASK, PHONE_MASK, sanitize_headers, sanitize_text

SHOPS = Path(__file__).resolve().parents[1] / "fixtures" / "shops"


@pytest.mark.parametrize(
    "ip",
    [
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "127.0.0.1",
        "0.0.0.0",  # noqa: S104 - a forbidden target, not a bind address
        "169.254.169.254",
        "100.64.1.1",
        "224.0.0.1",
        "240.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "fd12::1",
        "::ffff:10.0.0.1",
        "64:ff9b::a00:1",
        "ff02::1",
        "not-an-ip",
    ],
)
def test_forbidden_ips(ip: str) -> None:
    assert is_forbidden_ip(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700::6810:84e5", "1.1.1.1"])
def test_public_ips(ip: str) -> None:
    assert not is_forbidden_ip(ip)


def test_guard_blocks_private_resolution_and_bad_schemes() -> None:
    table = {
        "shop.de": ["93.184.216.34"],
        "evil.de": ["93.184.216.34", "10.0.0.5"],
        "meta.de": ["169.254.169.254"],
    }

    async def resolve(h: str) -> list[str]:
        return table.get(h, [])

    guard = EgressGuard(resolve)

    async def run() -> None:
        d = await guard.check("https://shop.de/")
        assert d.addresses == ("93.184.216.34",)
        for bad in (
            "https://evil.de/",
            "http://meta.de/latest",
            "ftp://shop.de/",
            "https://nx.de/",
            "http://127.0.0.1/",
            "https://shop.de:2222/",
            "https://[::1]/",
        ):
            with pytest.raises(EgressBlocked):
                await guard.check(bad)

    asyncio.run(run())
    lab = EgressGuard(resolve, allow_private=True)
    assert asyncio.run(lab.check("http://127.0.0.1:8765/")).addresses == ("127.0.0.1",)


def test_sanitize_removes_pii_and_keeps_numbers() -> None:
    html = (SHOPS / "pii" / "index.html").read_text(encoding="utf-8")
    clean, stats = sanitize_text(html)
    assert "@" not in clean.replace(EMAIL_MASK, "") or "sales@" not in clean
    assert (
        "sales@pii-shop.test" not in clean and "support.team+promo@mail.pii-shop.test" not in clean
    )
    assert "mailto:ceo@pii-shop.test" not in clean and "tel:+442079460958" not in clean
    for phone in ("+44 20 7946 0958", "+1 (415) 555-0132", "030 123456", "0049 30 1234567"):
        assert phone not in clean
    assert stats.emails == 3 and stats.phones == 5
    # numbers that are not phones survive (prices, order ids, EAN, years)
    for keep in ("199.99", "12345", "4006381333931", "2026"):
        assert keep in clean
    assert PHONE_MASK in clean and EMAIL_MASK in clean


def test_sanitize_headers() -> None:
    clean, stats = sanitize_headers({"server": "nginx", "x-contact": "ops@h.test, +49 30 1234567"})
    assert clean["server"] == "nginx" and clean["x-contact"] == f"{EMAIL_MASK}, {PHONE_MASK}"
    assert stats.emails == 1 and stats.phones == 1


def test_robots_semantics() -> None:
    body = (SHOPS / "woo-shop" / "robots.txt").read_text()
    token = agent_token("PayIntelBot/1.0 (+https://example.test/bot)")
    assert token == "PayIntelBot"
    rules = parse_robots(body, 200, agent_token=token)
    assert rules.allows("https://woo-shop.test/") and rules.allows(
        "https://woo-shop.test/warenkorb/"
    )
    assert not rules.allows("https://woo-shop.test/kasse/")  # our UA group
    assert rules.allows("https://woo-shop.test/mein-konto/")  # that rule is for other agents
    assert rules.crawl_delay == 1.0
    assert parse_robots(None, 404, agent_token=token).allows("https://x.test/anything")
    assert not parse_robots(None, 403, agent_token=token).allows("https://x.test/")
    assert not parse_robots(None, 503, agent_token=token).allows("https://x.test/")
    assert not parse_robots(None, None, agent_token=token).allows("https://x.test/")
