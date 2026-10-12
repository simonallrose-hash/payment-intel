"""Third-party script URL selection (FR-LS-03, ADR-0038) — pure part of the script pool."""

from __future__ import annotations

from payintel.crawl.light.scripts import third_party_script_urls


def test_third_party_urls_skip_own_etld1_dedupe_and_cap() -> None:
    srcs = [
        "https://shop.example.de/a.js",  # first party
        "https://cdn.shop.example.de/b.js",  # subdomain of the shop: first party
        "https://js.stripe.com/v3/",
        "https://js.stripe.com/v3/#frag",  # same URL once the fragment is dropped
        "https://JS.STRIPE.COM/v3/",  # host case differs: a different URL string, kept
        "data:text/javascript;base64,AAAA",  # not http(s)
        "//cdn.example.net/relative-scheme.js",  # no scheme: extract() resolves these first
        "https://www.paypal.com/sdk/js?client-id=test&currency=EUR",
        "https://cdn.shopify.com/s/files/1/app.js",
    ]
    out = third_party_script_urls(srcs, "example.de", limit=25)
    assert out == [
        "https://js.stripe.com/v3/",
        "https://JS.STRIPE.COM/v3/",
        "https://www.paypal.com/sdk/js?client-id=test&currency=EUR",
        "https://cdn.shopify.com/s/files/1/app.js",
    ]
    assert third_party_script_urls(srcs, "example.de", limit=2) == out[:2]
    assert third_party_script_urls(srcs, "example.de", limit=0) == []
    assert third_party_script_urls(["https://x.net/" + "a" * 2048], "example.de", limit=5) == []


def test_attribute_values_decode_like_a_browser() -> None:
    """`&currency=` in a src is not the ¤ entity: legacy references without ';' stay
    verbatim in attributes when '=' or a letter follows (the PayPal SDK URL case)."""
    from payintel.crawl.light.html import extract, unescape_attr

    assert (
        unescape_attr("a=1&currency=EUR&copy2024&amp;x=2&lt;") == "a=1&currency=EUR&copy2024&x=2<"
    )
    assert unescape_attr("&copy; 2024 &curren;") == "© 2024 ¤"
    page = (
        '<html><body><script src="https://www.paypal.com/sdk/js?client-id=t&currency=EUR"></script>'
        "<script src='/a.js?v=1&amp;b=2'></script><a href=\"/p?x=1&y=2\">p</a></body></html>"
    )
    f = extract(page, "https://shop.example/")
    assert f.scripts_src == [
        "https://www.paypal.com/sdk/js?client-id=t&currency=EUR",
        "https://shop.example/a.js?v=1&b=2",
    ]
    assert f.links == ["https://shop.example/p?x=1&y=2"]
