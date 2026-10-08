"""Protection-page detection (FR-CW-06, AC-05): recognised without ever trying to pass it."""

from __future__ import annotations

import pytest

from payintel.crawl.checkout.blocking import detect_block, has_captcha_widget

LONG = "Lorem ipsum dolor sit amet. " * 200  # > SMALL_PAGE_CHARS


def test_http_403_and_429_are_blocks_with_vendor_when_known() -> None:
    b = detect_block(status=403, title="Access Denied", html="<p>errors.edgesuite.net</p>", text="")
    assert b and b.reason == "http_403_429" and b.blocked_by == "akamai"
    b = detect_block(status=429, title="", html="<p>slow down</p>", text="", url="https://x/")
    assert b and b.reason == "http_403_429" and b.blocked_by == "http_429"


def test_cloudflare_challenge_page() -> None:
    html = (
        '<div id="cf-challenge-running"></div><script src="/cdn-cgi/challenge-platform/x"></script>'
    )
    b = detect_block(status=503, title="Just a moment...", html=html, text="Checking your browser")
    assert b and b.reason == "antibot_challenge" and b.blocked_by == "cloudflare"
    b = detect_block(status=200, title="Shop", html=html, text="short")
    assert b and b.reason == "antibot_challenge"


def test_captcha_only_when_it_dominates_the_page() -> None:
    widget = '<div class="h-captcha" data-sitekey="k"></div>'
    b = detect_block(status=200, title="Verify you are human", html=widget, text="Verify")
    assert b and b.reason == "captcha" and b.blocked_by == "hcaptcha"
    b = detect_block(status=200, title="Login", html=widget + LONG, text=LONG)
    assert b is None
    assert has_captcha_widget(widget + LONG) == "hcaptcha"
    assert has_captcha_widget(LONG) is None


@pytest.mark.parametrize(
    "text",
    [
        "Sorry, this shop is not available in your country.",
        "Dieser Artikel ist nicht in Ihrem Land verfügbar.",
        "Désolé, ce site n'est pas disponible dans votre pays.",
    ],
)
def test_geo_block(text: str) -> None:
    b = detect_block(status=200, title="Shop", html=f"<p>{text}</p>", text=text)
    assert b and b.reason == "geo_blocked"


def test_age_gate_and_normal_pages() -> None:
    b = detect_block(
        status=200, title="Age verification", html="<p>x</p>", text="Are you over 18? " + LONG
    )
    assert b and b.reason == "age_gate"
    assert detect_block(status=200, title="Shop", html=LONG, text=LONG) is None
    assert detect_block(status=None, title="", html="", text="") is None
