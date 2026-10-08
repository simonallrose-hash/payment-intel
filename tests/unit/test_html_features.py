"""HTML extractor (FR-LS-05 inputs), parking (FR-DS-07) and classifier (FR-DS-08) on fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from payintel.core.models.base import ConfidenceLevel, DomainStatus
from payintel.crawl.light.html import extract
from payintel.discovery.classify import ClassifierInput, EcommerceClassifier
from payintel.discovery.parking import ParkingDetector

SHOPS = Path(__file__).resolve().parents[1] / "fixtures" / "shops"


def _load(name: str, host: str) -> str:
    return (SHOPS / name / "index.html").read_text(encoding="utf-8")


def test_extract_woo_home() -> None:
    f = extract(_load("woo-shop", "woo-shop.test"), "https://woo-shop.test/")
    assert f.title.startswith("Beispiel Laden")
    assert f.lang == "de-de" and f.hreflangs == ["de", "en"]
    assert "https://js.stripe.com/v3/" in f.scripts_src
    assert f.scripts_src[0] == "https://woo-shop.test/wp-includes/js/jquery/jquery.min.js"
    assert f.meta["generator"].startswith("WooCommerce")
    assert f.favicon == "https://woo-shop.test/favicon.ico"
    assert "wc_add_to_cart_params" in f.js_globals() and "dataLayer" in f.js_globals()
    assert "Organization" in f.json_ld_types and "PostalAddress" in f.json_ld_types
    assert "in den warenkorb" in f.button_texts
    assert "/warenkorb/" in f.link_paths() and "/kasse/" in f.link_paths()
    assert f.external_hosts() == {"js.stripe.com"}
    assert f.external_link_hosts() == {"www.instagram.com", "partner-shop.test"}
    assert "89,90 €" in f.text


def test_extract_shopify_and_blog() -> None:
    s = extract(_load("shopify-shop", "x"), "https://shopify-shop.test/")
    assert "Shopify" in s.js_globals()
    assert s.favicon == "https://cdn.shopify.com/s/files/1/0001/favicon.png"
    assert "Product" in s.json_ld_types and "add to cart" in s.button_texts
    b = extract(_load("blog", "x"), "https://blog.test/")
    assert b.lang == "fr" and b.json_ld_types == [] and "à propos" in b.button_texts


def test_extract_is_robust_to_broken_html() -> None:
    f = extract("<html><body><a href='/x'>x<div><script>var A = 1;</script><p>t", "https://h.test/")
    assert f.links == ["https://h.test/x"] and "A" in f.js_globals()


@pytest.fixture(scope="module")
def classifier() -> EcommerceClassifier:
    return EcommerceClassifier()


def test_classifier_verdicts(classifier: EcommerceClassifier) -> None:
    woo = extract(_load("woo-shop", "x"), "https://woo-shop.test/")
    c = classifier.classify(
        ClassifierInput(woo, platform_id="woocommerce", platform_confidence=ConfidenceLevel.HIGH)
    )
    assert c.status == DomainStatus.ECOMMERCE and c.confidence == ConfidenceLevel.HIGH
    assert {"platform:woocommerce", "add_to_cart", "cart_path", "checkout_path", "currency"} <= set(
        c.signals
    )
    # without platform knowledge the page still scores as a shop
    c2 = classifier.classify(ClassifierInput(woo))
    assert c2.status == DomainStatus.ECOMMERCE and c2.score < c.score
    blog = extract(_load("blog", "x"), "https://blog.test/")
    b = classifier.classify(
        ClassifierInput(blog, platform_id="wordpress", platform_confidence=ConfidenceLevel.HIGH)
    )
    # wordpress is a CMS, but the classifier only gets platform ids that are shops;
    # here we pass it deliberately: platform alone must not be enough to call it a shop
    assert b.score == pytest.approx(0.45) and b.status == DomainStatus.CANDIDATE
    assert classifier.classify(ClassifierInput(blog)).status == DomainStatus.NOT_ECOMMERCE
    shop = extract(_load("shopify-shop", "x"), "https://shopify-shop.test/")
    s = classifier.classify(
        ClassifierInput(shop, platform_id="shopify", platform_confidence=ConfidenceLevel.MEDIUM)
    )
    assert s.status == DomainStatus.ECOMMERCE and "schema_org" in s.signals


def test_parking_signatures() -> None:
    det = ParkingDetector()
    assert (
        det.check_dns(ns=["ns1.sedoparking.com", "ns2.sedoparking.com"], cname=None).signature_id
        == "sedo_ns"
    )
    assert not det.check_dns(ns=["ns1.example-dns.net"], cname="cdn.example.net").parked
    html = _load("parked", "x")
    v = det.check_html(html, link_count=0, max_stub_bytes=1500)
    assert v.parked and v.signature_id == "sedo_html"
    assert (
        det.check_html(
            "<html><body>Soon</body></html>", link_count=0, max_stub_bytes=1500
        ).signature_id
        == "stub_page"
    )
    assert not det.check_html(
        "<html><body>Soon</body></html>", link_count=3, max_stub_bytes=1500
    ).parked
    assert not det.check_html(_load("woo-shop", "x"), link_count=9, max_stub_bytes=1500).parked
    # unverified signatures are listed but never applied
    assert {s.id for s in det.needing_verification()} == {"uniregistry_ns", "namebright_ns"}
    assert not det.check_dns(ns=["a.uniregistrymarket.link"], cname=None).parked
