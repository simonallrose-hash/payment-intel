"""Rule engine + scoring (FR-DT-01/02/04/05) and country detection (FR-DT-09) on fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from payintel.core.models.base import ConfidenceLevel, RuleTargetType, SignalType
from payintel.core.reference_loader import load_reference
from payintel.crawl.light.html import extract
from payintel.detect.country import CountryDetector
from payintel.detect.engine import PageSignals, match_page, match_pages
from payintel.detect.rules import RuleSet, load_rules
from payintel.detect.scoring import aggregate, best_platform

SHOPS = Path(__file__).resolve().parents[1] / "fixtures" / "shops"


@pytest.fixture(scope="module")
def ruleset() -> RuleSet:
    return load_rules(reference=load_reference())


def _signals(
    name: str, page_type: str, url: str, headers: dict[str, str] | None = None
) -> PageSignals:
    html = (SHOPS / name / "index.html").read_text(encoding="utf-8")
    f = extract(html, url)
    return PageSignals(
        page_type=page_type,
        url=url,
        html=html,
        headers=headers or {},
        script_srcs=f.scripts_src,
        iframe_srcs=f.iframes_src,
        form_actions=f.forms_action,
        network_hosts=f.external_hosts(),
        js_globals=f.js_globals(),
    )


def test_woo_home_findings_and_scores(ruleset: RuleSet) -> None:
    home = _signals("woo-shop", "homepage", "https://woo-shop.test/", {"x-powered-by": "PHP/8.3"})
    findings = match_page(ruleset, home)
    ids = {f.rule.rule_id for f in findings}
    assert "platform.woocommerce.html_pattern.1" in ids
    assert "platform.woocommerce.js_global.1" in ids
    assert any(f.rule.target_id == "stripe" for f in findings)
    assert any(f.rule.target_id == "wordpress" for f in findings)
    scores = aggregate(findings)
    plat = best_platform(scores)
    assert plat is not None and plat.target_id == "woocommerce"
    assert plat.confidence == ConfidenceLevel.HIGH  # ≥2 independent signal types
    assert {SignalType.HTML_PATTERN, SignalType.JS_GLOBAL} <= plat.signal_types
    stripe = next(s for s in scores if s.target_id == "stripe")
    assert stripe.confidence == ConfidenceLevel.LOW  # homepage only (FR-DT-04)
    assert stripe.active_on_checkout is False  # FR-DT-05
    assert 0 < stripe.score <= 1.0
    for f in findings:
        assert (
            f.page_type == "homepage" and f.page_url == "https://woo-shop.test/" and f.signal_value
        )


def test_checkout_signal_raises_confidence(ruleset: RuleSet) -> None:
    home = _signals("woo-shop", "homepage", "https://woo-shop.test/")
    checkout = PageSignals(
        page_type="checkout",
        url="https://woo-shop.test/kasse/",
        network_hosts={"api.stripe.com", "js.stripe.com"},
        js_globals={"Stripe"},
    )
    scores = aggregate(match_pages(ruleset, [home, checkout]))
    stripe = next(s for s in scores if s.target_id == "stripe")
    assert stripe.confidence == ConfidenceLevel.HIGH and stripe.active_on_checkout
    medium = aggregate(
        match_page(
            ruleset,
            PageSignals(
                page_type="checkout",
                url="https://x.test/checkout",
                script_srcs=["https://js.stripe.com/v3/"],
            ),
        )
    )
    s = next(x for x in medium if x.target_id == "stripe")
    assert s.confidence in {ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH} and s.active_on_checkout


def test_shopify_and_blog(ruleset: RuleSet) -> None:
    sh = aggregate(
        match_page(ruleset, _signals("shopify-shop", "homepage", "https://shopify-shop.test/"))
    )
    plat = best_platform(sh)
    assert plat is not None and plat.target_id == "shopify"
    assert any(s.target_id == "paypal" and s.target_type == RuleTargetType.PROVIDER for s in sh)
    blog = aggregate(match_page(ruleset, _signals("blog", "homepage", "https://blog.test/")))
    assert best_platform(blog) is not None and best_platform(blog).target_id == "wordpress"  # type: ignore[union-attr]
    assert not any(s.target_type == RuleTargetType.PROVIDER for s in blog)


def test_header_cookie_favicon_matching() -> None:
    from payintel.core.models.base import PageScope
    from payintel.detect.rules import Rule

    def rule(sig: SignalType, pattern: str, match: str, rid: str = "tech.x.r.1") -> Rule:
        return Rule(
            rid,
            1,
            RuleTargetType.TECH,
            "x",
            sig,
            pattern,
            match,
            0.5,
            PageScope.ANY,
            True,
            False,
            "t",
        )

    rs = RuleSet(
        rules=(
            rule(SignalType.HEADER, "x-shopify-stage", "header_name", "tech.x.h.1"),
            rule(SignalType.HEADER, "server: ^cloudflare", "header_name", "tech.x.h.2"),
            rule(SignalType.COOKIE, "PHPSESSID", "cookie_name", "tech.x.c.1"),
            rule(SignalType.FAVICON, "a" * 64, "sha256", "tech.x.f.1"),
            rule(SignalType.NETWORK_HOST, "*.stripe.com", "host_suffix", "tech.x.n.1"),
            rule(SignalType.JS_GLOBAL, "Shopify.PaymentButton", "exact", "tech.x.j.1"),
        )
    )
    sig = PageSignals(
        page_type="homepage",
        url="https://h.test/",
        headers={"x-shopify-stage": "production", "server": "cloudflare"},
        cookie_names={"PHPSESSID"},
        favicon_sha256="a" * 64,
        network_hosts={"m.stripe.com"},
        js_globals={"Shopify.PaymentButton.init"},
    )
    got = {f.rule.rule_id: f.signal_value for f in match_page(rs, sig)}
    assert set(got) == {
        "tech.x.h.1",
        "tech.x.h.2",
        "tech.x.c.1",
        "tech.x.f.1",
        "tech.x.n.1",
        "tech.x.j.1",
    }
    assert got["tech.x.n.1"] == "m.stripe.com"
    # page scope is respected
    co = Rule(
        "tech.x.r.2",
        1,
        RuleTargetType.TECH,
        "x",
        SignalType.COOKIE,
        "PHPSESSID",
        "cookie_name",
        0.5,
        PageScope.CHECKOUT,
        True,
        False,
        "t",
    )
    assert match_page(RuleSet(rules=(co,)), sig) == []


def test_country_detection() -> None:
    det = CountryDetector()
    woo = extract(
        (SHOPS / "woo-shop" / "index.html").read_text(encoding="utf-8"), "https://woo-shop.test/"
    )
    r = det.detect(woo, etld1="woo-shop.de", raw_text=woo.text)
    assert r.country == "DE" and r.confidence == ConfidenceLevel.HIGH and r.currency == "EUR"
    assert {"cctld:de", "currency:EUR", "lang:de-de", "schema_address:DE"} <= set(r.signals)
    sh = extract(
        (SHOPS / "shopify-shop" / "index.html").read_text(encoding="utf-8"),
        "https://shopify-shop.test/",
    )
    r2 = det.detect(sh, etld1="shopify-shop.com", raw_text=sh.text)
    assert (
        r2.country == "GB"
        and r2.currency == "GBP"
        and r2.confidence in {ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH}
    )
    blog = extract(
        (SHOPS / "blog" / "index.html").read_text(encoding="utf-8"), "https://blog.test/"
    )
    r3 = det.detect(blog, etld1="blog.com", raw_text=blog.text)
    assert r3.country == "FR" and r3.currency is None  # lang fr + phone +33
    empty = det.detect(extract("<html></html>", "https://x.io/"), etld1="x.io")
    assert empty.country is None and empty.confidence is None
    assert (
        det.detect(
            extract("<html></html>", "https://x.io/"), etld1="x.io", hosting_country="nl"
        ).country
        == "NL"
    )


def test_two_signal_types_without_checkout_stay_low(ruleset: RuleSet) -> None:
    """ADR-0035: two independent signal types on the homepage are not `high` for a provider."""
    home = PageSignals(
        page_type="homepage",
        url="https://x.test/",
        script_srcs=["https://js.stripe.com/v3/"],
        js_globals={"Stripe"},
    )
    scores = aggregate(match_page(ruleset, home))
    stripe = next(s for s in scores if s.target_id == "stripe")
    assert len(stripe.signal_types) >= 2
    assert stripe.confidence == ConfidenceLevel.LOW and not stripe.active_on_checkout


def test_every_payment_method_has_a_label_rule_and_local_methods_exceed_20(
    ruleset: RuleSet,
) -> None:
    """FR-DT-03: the starter set covers ≥ 20 local (non-global) payment methods, and every
    method of the reference has at least one enabled rule."""
    ref = load_reference()
    with_rules = {
        r.target_id
        for r in ruleset.rules
        if r.target_type == RuleTargetType.PAYMENT_METHOD and r.enabled
    }
    missing = {m["id"] for m in ref.payment_methods} - with_rules
    assert not missing, missing
    local = {m["id"] for m in ref.payment_methods if "global" not in m.get("regions", [])}
    assert len(local & with_rules) >= 20, sorted(local & with_rules)


def test_checkout_labels_match_local_methods_but_not_lookalikes(ruleset: RuleSet) -> None:
    """Label rules are regexes over the payment-option texts captured at the checkout; the
    ambiguous brand names (Zip, Discover, Alma, CB) must not fire on everyday words."""

    def methods_for(*labels: str) -> set[str]:
        sig = PageSignals(
            page_type="checkout", url="https://shop.example/checkout", checkout_labels=list(labels)
        )
        return {
            f.rule.target_id
            for f in match_page(ruleset, sig)
            if f.rule.target_type == RuleTargetType.PAYMENT_METHOD
        }

    found = methods_for(
        "eps-Überweisung",
        "Trustly",
        "MobilePay",
        "Vipps",
        "Swish",
        "Multibanco",
        "Payer en 3 fois avec Alma",
        "Scalapay",
        "Riverty Rechnung",
        "Pay with Zip",
        "UPI / BHIM",
        "Carte bancaire / CB",
        "Vorkasse (Banküberweisung)",
        "Direct Debit",
        "Pay by Bank app",
        "Bancontact card",
        "PayPal Pay in 3",
        "Gutscheinkarte",
        "Bitcoin",
        "Discover",
        "JCB",
        "UnionPay",
        "Maestro",
        "Samsung Pay",
        "Alipay",
        "WeChat Pay",
        "MyBank",
    )
    assert found >= {
        "eps",
        "trustly",
        "mobilepay",
        "vipps",
        "swish",
        "multibanco",
        "alma",
        "scalapay",
        "riverty",
        "zip",
        "upi",
        "cartes_bancaires",
        "sepa_credit_transfer",
        "bacs_direct_debit",
        "open_banking",
        "bancontact_card",
        "paypal_pay_later",
        "gift_card",
        "crypto_generic",
        "discover",
        "jcb",
        "unionpay",
        "maestro",
        "samsung_pay",
        "alipay",
        "wechat_pay",
        "mybank",
    }
    assert "sepa_direct_debit" not in methods_for("Direct Debit")
    assert "zip" not in methods_for("ZIP code", "Zip / postal code")
    assert "discover" not in methods_for("Discover more products")
    assert "bacs_direct_debit" not in methods_for("SEPA Direct Debit")
