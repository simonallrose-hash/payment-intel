"""FR-CW-11: country-specific walk parameters and geolocation-only proxies."""

from __future__ import annotations

import pytest

from payintel.crawl.checkout import geo
from payintel.crawl.checkout.browser import ContextOptions, context_kwargs
from payintel.crawl.checkout.identities import IdentityProvider
from payintel.crawl.checkout.walker import WalkInput

PROXIES = {"FR": "http://user:s3cret@fr-proxy.example:3128", "nl": "socks5://nl.example:1080"}


def test_choose_uses_a_proxy_only_for_another_country() -> None:
    home = geo.choose("de", egress_country="DE", proxies=PROXIES)
    assert home == geo.GeoChoice("DE", None, geo.REASON_EGRESS)
    fr = geo.choose("FR", egress_country="DE", proxies=PROXIES)
    assert fr.reason == geo.REASON_PROXY and fr.proxy is not None
    assert fr.proxy.as_playwright() == {
        "server": "http://fr-proxy.example:3128",
        "username": "user",
        "password": "s3cret",
    }
    assert fr.as_log() == {
        "country": "FR",
        "proxy": "http://fr-proxy.example:3128",
        "reason": geo.REASON_PROXY,
    }
    nl = geo.choose("NL", egress_country="DE", proxies=PROXIES)
    assert nl.proxy is not None and nl.proxy.as_playwright() == {
        "server": "socks5://nl.example:1080"
    }
    es = geo.choose("ES", egress_country="DE", proxies=PROXIES)
    assert es == geo.GeoChoice("ES", None, geo.REASON_NO_PROXY)
    assert geo.choose("FR", egress_country="DE", proxies={}).proxy is None
    with pytest.raises(ValueError):
        geo.ProxyConfig.parse("ftp://x.example")
    with pytest.raises(ValueError):
        geo.ProxyConfig.parse("not a url")


def test_context_keeps_the_identifying_user_agent_with_or_without_proxy() -> None:
    ids = IdentityProvider(company_domain="payintel.test", company_phone="+49 30 000000")
    fr = ids.for_country("FR", email_token="h1")
    opts = ContextOptions(locale=fr.locale, accept_language=fr.accept_language)
    plain = context_kwargs(
        opts, user_agent="PayIntelBot/1.0 (+https://x/bot)", viewport=(1366, 900)
    )
    assert "proxy" not in plain and plain["locale"] == fr.locale
    assert plain["extra_http_headers"]["Accept-Language"].startswith(fr.locale)
    choice = geo.choose("FR", egress_country="DE", proxies=PROXIES)
    assert choice.proxy is not None
    with_proxy = context_kwargs(
        ContextOptions(
            locale=fr.locale, accept_language=fr.accept_language, proxy=choice.proxy.as_playwright()
        ),
        user_agent="PayIntelBot/1.0 (+https://x/bot)",
        viewport=(1366, 900),
    )
    assert with_proxy["proxy"] == choice.proxy.as_playwright()
    assert with_proxy["user_agent"] == plain["user_agent"]  # the proxy never changes who we are
    assert {k: v for k, v in with_proxy.items() if k != "proxy"} == plain


def test_walk_input_carries_the_geo_decision() -> None:
    ids = IdentityProvider(company_domain="payintel.test", company_phone="+49 30 000000")
    identity = ids.for_country("FR", email_token="h2")
    choice = geo.choose(identity.country, egress_country="DE", proxies=PROXIES)
    inp = WalkInput(
        url="https://shop.example/",
        etld1="shop.example",
        identity=identity,
        flags=None,  # type: ignore[arg-type]
        url_check=lambda _u: None,
        proxy=choice.proxy.as_playwright() if choice.proxy else None,
        geo=choice.as_log(),
    )
    assert inp.proxy is not None and inp.proxy["server"] == "http://fr-proxy.example:3128"
    assert inp.geo["proxy"] == "http://fr-proxy.example:3128" and "s3cret" not in str(inp.geo)
