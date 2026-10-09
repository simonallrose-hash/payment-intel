"""`reference/hosted_checkouts.yaml` (ADR-0032): verified hosts, matching, rule coverage."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from payintel.core.errors import ReferenceError_
from payintel.core.hosted_checkouts import HostedCheckout, HostedCheckouts
from payintel.core.reference_loader import REFERENCE_DIR, load_reference
from payintel.detect.hosts import HostCategorizer
from payintel.detect.rules import load_rules


def test_reference_loads_with_verified_and_unverified_hosts() -> None:
    ref = load_reference()
    hosted = ref.hosted_checkouts
    assert len(hosted.verified) >= 17
    assert len(hosted.entries) > len(hosted.verified)  # the unverifiable ones stay on record
    ids = {p["id"] for p in ref.providers}
    assert hosted.provider_ids() <= ids
    assert all(e.source.startswith("https://") for e in hosted.entries)
    assert all(e.host == e.host.lower() for e in hosted.entries)
    # Shopify is a platform adapter case (ADR-0029/0032), never a hosted host
    assert not any("shopify" in e.host for e in hosted.entries)


def test_every_verified_host_is_also_a_psp_host_for_the_categoriser() -> None:
    """A hosted host must be attributable through an enabled `network_host` rule of the same
    provider: that rule produces the observation when the walk stops there."""
    ref = load_reference()
    cat = HostCategorizer(load_rules(reference=ref))
    for e in ref.hosted_checkouts.verified:
        got = cat.classify(e.host)
        assert (got.category, got.provider_id) == ("psp", e.provider_id), e


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://checkout.stripe.com/c/pay/cs_test_1#x", "stripe"),
        ("https://buy.stripe.com/test_cN25nr0iZ7bUa7meUY", "stripe"),
        ("https://www.paypal.com/cgi-bin/webscr?cmd=_cart", "paypal"),
        ("https://www.paypal.com/checkoutnow?token=5O190127TN364715T", "paypal"),
        ("https://www.paypal.com/sdk/js?client-id=x", None),  # the SDK, not a checkout
        ("https://payment-links.mollie.com/payment/4Y0eZitmBnQ6IDoMqZQKh", "mollie"),
        ("https://www.mollie.com/checkout/select-method/abc", None),  # needs_verification
        ("https://square.link/u/EXAMPLE", "square"),
        ("https://checkout.square.site/EXAMPLE", "square"),
        ("https://api.flutterwave.com/v3/hosted/pay/f524c1196ffda5556341", "flutterwave"),
        ("https://api.flutterwave.com/v3/payments", None),  # the API, outside the prefix
        ("https://rzp.io/i/nxrHnLJ", "razorpay"),
        ("https://www.mercadopago.com/mla/checkout/start?pref_id=1", "mercado_pago"),
        ("https://www.vivapayments.com/web/checkout?ref=123", "viva_wallet"),
        ("https://test.checkout.dibspayment.eu/hostedpaymentpage/?check=1", "nexi"),
        ("https://checkout.dibspayment.eu/hostedpaymentpage/?check=1", None),  # unverified
        ("https://evil.checkout.stripe.com/", None),  # exact host, no suffix match
        ("https://CHECKOUT.STRIPE.COM/c/pay/x", "stripe"),
        ("not a url", None),
    ],
)
def test_match_only_verified_hosts_within_their_documented_paths(
    url: str, provider: str | None
) -> None:
    hit = load_reference().hosted_checkouts.match(url)
    assert (hit.provider_id if hit else None) == provider


def test_longest_path_prefix_wins_and_unverified_entries_never_match() -> None:
    entries = (
        HostedCheckout("pay.example", "a", "verified", "https://x", path_prefix="/"),
        HostedCheckout("pay.example", "b", "verified", "https://x", path_prefix="/b/"),
        HostedCheckout("pay.example", "c", "needs_verification", "https://x", path_prefix="/b/c/"),
    )
    hosted = HostedCheckouts(entries)
    assert hosted.match("https://pay.example/b/c/d").provider_id == "b"  # type: ignore[union-attr]
    assert hosted.match("https://pay.example/a").provider_id == "a"  # type: ignore[union-attr]
    assert HostedCheckouts.empty().match("https://pay.example/b/") is None


def test_unknown_provider_id_is_rejected(tmp_path: Path) -> None:
    copy = tmp_path / "reference"
    shutil.copytree(REFERENCE_DIR, copy)
    path = copy / "hosted_checkouts.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["hosts"].append(
        {
            "host": "pay.nobody.example",
            "provider_id": "nobody",
            "status": "verified",
            "source": "https://nobody.example/docs",
        }
    )
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ReferenceError_, match="hosted_checkouts.yaml: unknown provider ids"):
        load_reference(copy)
    doc["hosts"][-1]["status"] = "verified!"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ReferenceError_, match="schema validation failed"):
        load_reference(copy)
