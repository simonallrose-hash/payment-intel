"""Stage-2 reference data: payment test values (ADR-0003), identities (FR-CW-05),
checkout dictionary (FR-CW-03/04) and host categories (FR-DT-11)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from payintel.crawl.checkout.dictionary import contains_phrase, load_dictionary, normalize
from payintel.crawl.checkout.identities import (
    IDENTITIES_DIR,
    IdentityProvider,
    load_identity_sets,
    probe_email,
)
from payintel.crawl.checkout.payment_values import load_payment_values, luhn_valid
from payintel.detect.hosts import HostCategorizer
from payintel.detect.rules import load_rules


def test_payment_values_only_documented_or_luhn_invalid() -> None:
    v = load_payment_values()
    assert v.numbers  # the guardrail allow-list
    for n, brand in v.documented.items():
        assert luhn_valid(n) and brand and v.sources[n].startswith("https://")
    for n in v.luhn_invalid:
        assert not luhn_valid(n)
    assert set(v.preferred) <= v.numbers
    assert v.allowed("4242424242424242") and not v.allowed("4242 4242 4242 4242")
    assert not v.allowed("4916338506082832")  # a random Luhn-valid number is never allowed
    mm, yy, yyyy = v.expiry(2026)
    assert mm == "12" and yyyy == "2030" and yy == "30"
    assert v.cvc_for("378282246310005") == "1234" and v.cvc_for("4242424242424242") == "123"
    assert v.number(brand="mastercard") in v.documented


def test_payment_values_loader_rejects_luhn_valid_in_invalid_list(tmp_path: Path) -> None:
    doc = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "reference" / "test_payment_values.yaml").read_text()
    )
    doc["cards"]["luhn_invalid"].append("4242424242424242")
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(doc))
    from payintel.crawl.checkout.payment_values import _load

    with pytest.raises(ValueError, match="passes Luhn"):
        _load(p)


def test_identities_are_synthetic_and_cover_target_countries() -> None:
    sets = load_identity_sets()
    assert {"DE", "GB", "US", "FR", "NL", "ES", "IT", "PL", "PT", "SE", "DK"} <= set(sets)
    for cc, doc in sets.items():
        assert doc["country"] == cc
        for name in doc["first_names"] + doc["last_names"]:
            assert name.lower().startswith(("test", "probe")), name  # explicitly fictional
        if doc["phone"]["kind"] == "fictional_range":
            assert doc["phone"]["numbers"] and doc["phone"].get("source")
    prov = IdentityProvider(company_domain="payintel.example", company_phone="+44 20 7946 0000")
    de = prov.for_country("de", email_token="abc-123")
    assert de.country == "DE" and de.email == "checkout-probe+abc-123@payintel.example"
    assert de.phone == "+44 20 7946 0000" and de.postcode == "10115"
    gb = prov.for_country("GB", email_token="h42")
    assert gb.phone.startswith("+44 7700 900") or gb.phone.startswith("+44 20 7946")
    assert prov.for_country("ZZ", email_token="x").country == "DE"  # fallback
    assert prov.for_country("DE", email_token="x", variant=1).city == "München"
    assert probe_email("a b/c", "d.example") == "checkout-probe+abc@d.example"
    assert "Test" in de.as_log()["name"] and de.accept_language.startswith("de-DE")
    assert IDENTITIES_DIR.is_dir()


def test_dictionary_normalisation_and_matching() -> None:
    d = load_dictionary()
    assert len(d.languages) == 10
    assert normalize("  Zahlungspflichtig   BESTELLEN! ") == "zahlungspflichtig bestellen"
    assert normalize("Place order →") == "place order"
    assert contains_phrase("now place order please", "place order")
    assert not contains_phrase("continue to payment", "pay")
    assert d.match("Jetzt kaufen", "final_actions") == "jetzt kaufen"
    assert d.match("Commander", "final_actions") == "commander"
    assert d.match("Continue to payment", "final_actions") is None
    assert d.match("Continue to payment", "step_actions") == "continue to payment"
    assert d.match("Weiter zur Kasse", "step_actions") == "weiter zur kasse"
    assert d.match("Ajouter au panier", "add_to_cart") == "ajouter au panier"
    assert d.match("Checkout as guest", "guest_checkout") == "checkout as guest"
    assert d.match("Subscribe to our newsletter", "forbidden_consents") == "newsletter"
    assert d.match("I agree to the terms", "required_consents") in {"i agree", "terms"}
    assert d.marker_in("button button-primary place_order", d.final_markers) == "place_order"
    assert d.marker_in("/?wc-ajax=checkout", d.order_form_markers) == "wc-ajax=checkout"
    for lang in d.languages:
        assert d.by_language["final_actions"][lang] and d.by_language["step_actions"][lang]
    # no phrase is both a step and a final action (that would make every click ambiguous)
    assert not set(d.phrases("final_actions")) & set(d.phrases("step_actions"))


def test_host_categories() -> None:
    hc = HostCategorizer(load_rules())
    assert hc.classify("www.google-analytics.com").category == "analytics"
    assert hc.classify("googletagmanager.com").category == "tag_manager"
    assert hc.classify("static.hotjar.com").category == "session_replay"
    assert hc.classify("widget.intercom.io").category == "chat"
    assert hc.classify("cdnjs.cloudflare.com").category == "cdn"
    assert hc.classify("connect.facebook.net").category == "ads"
    stripe = hc.classify("js.stripe.com")
    assert stripe.category == "psp" and stripe.provider_id == "stripe"
    assert stripe.etld1 == "stripe.com"
    unknown = hc.classify("cdn.some-shop-tool.co.uk")
    assert unknown.category == "unknown" and unknown.etld1 == "some-shop-tool.co.uk"
