"""Reference dictionaries are valid and complete (FR-NR-01..03, FR-DT-03, 5.3)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from payintel.core.errors import ReferenceError_
from payintel.core.models.base import PaymentMethodType, ProviderRole
from payintel.core.reference_loader import REFERENCE_DIR, load_reference

TAXONOMY_53 = {
    "card": {
        "visa",
        "mastercard",
        "amex",
        "discover",
        "jcb",
        "unionpay",
        "cartes_bancaires",
        "bancontact_card",
        "maestro",
    },
    "wallet": {
        "apple_pay",
        "google_pay",
        "paypal",
        "amazon_pay",
        "shop_pay",
        "samsung_pay",
        "alipay",
        "wechat_pay",
    },
    "bnpl": {
        "klarna",
        "afterpay_clearpay",
        "affirm",
        "zip",
        "paypal_pay_later",
        "scalapay",
        "alma",
        "riverty",
    },
    "bank_redirect": {
        "ideal",
        "bancontact",
        "blik",
        "przelewy24",
        "eps",
        "trustly",
        "sofort_klarna",
        "mybank",
        "twint",
        "mobilepay",
        "vipps",
        "swish",
    },
    "bank_transfer": {"sepa_credit_transfer", "open_banking"},
    "direct_debit": {"sepa_direct_debit", "bacs_direct_debit"},
    "cash_voucher": {"boleto", "oxxo", "multibanco"},
    "real_time_payment": {"pix", "upi"},
    "crypto": {"crypto_generic"},
    "cod": {"cash_on_delivery"},
    "other": {"invoice", "gift_card"},
}
REQUIRED_PSPS = {
    "stripe",
    "adyen",
    "paypal",
    "braintree",
    "checkout_com",
    "worldpay",
    "mollie",
    "klarna",
    "square",
    "authorize_net",
    "global_payments",
    "nexi",
    "worldline",
    "unzer",
    "computop",
    "payone",
    "paysafe",
    "shopify_payments",
    "amazon_pay",
    "elavon",
    "fiserv",
    "cybersource",
    "rapyd",
    "payu",
    "przelewy24",
    "viva_wallet",
    "sumup",
    "buckaroo",
    "multisafepay",
    "pay_nl",
    "redsys",
    "stancer",
    "monext",
    "novalnet",
    "razorpay",
    "mercado_pago",
}
ORCHESTRATORS = {"spreedly", "primer", "gr4vy", "cellpoint_digital", "apexx"}


def test_reference_loads() -> None:
    ref = load_reference()
    assert len(ref.providers) >= 30  # FR-DT-03
    assert len(ref.payment_methods) >= 20  # FR-DT-03
    assert len(ref.verticals) == 20  # FR-DT-10
    assert len(ref.platforms) >= 7  # FR-CW-03 adapters at least


def test_required_psps_and_orchestrators_present() -> None:
    ref = load_reference()
    ids = {p["id"] for p in ref.providers}
    assert REQUIRED_PSPS <= ids
    assert ORCHESTRATORS <= ids
    for p in ref.providers:
        if p["id"] in ORCHESTRATORS:
            assert p["role"] == ProviderRole.ORCHESTRATOR.value  # FR-DT-06


def test_payment_method_taxonomy_matches_5_3() -> None:
    ref = load_reference()
    by_type: dict[str, set[str]] = {}
    for m in ref.payment_methods:
        by_type.setdefault(m["type"], set()).add(m["id"])
    for mtype, ids in TAXONOMY_53.items():
        PaymentMethodType(mtype)
        assert ids <= by_type[mtype], f"missing {ids - by_type[mtype]} in type {mtype}"


def test_brand_owner_links() -> None:
    ref = load_reference()
    braintree = next(p for p in ref.providers if p["id"] == "braintree")
    assert braintree["parent_provider_id"] == "paypal"  # FR-NR-01 example


def test_stop_reason_taxonomy() -> None:
    ref = load_reference()
    sr = ref.stop_reasons
    assert set(sr.steps) == {
        "navigation",
        "protection",
        "product",
        "cart",
        "checkout",
        "registration",
        "address",
        "shipping",
        "payment",
    }
    assert "guardrail_final_action_only" in sr.steps["payment"]  # FR-CW-04 → FR-CW-13
    assert "guardrail_ambiguous_button" in sr.steps["payment"]
    assert sr.is_valid("payment", "guardrail_final_action_only", None)
    assert sr.is_valid("cart", "other", "something specific")
    assert not sr.is_valid("cart", "other", "")  # `other` requires stop_detail
    assert not sr.is_valid("cart", "captcha", None)  # wrong step
    assert not sr.is_valid("unknown", "other", "x")


# --- invalid dictionaries are rejected ----------------------------------------


@pytest.fixture
def ref_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "reference"
    shutil.copytree(REFERENCE_DIR, dst)
    return dst


def _mutate(path: Path, fn: object) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(doc)  # type: ignore[operator]
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")


def test_duplicate_provider_id_rejected(ref_copy: Path) -> None:
    _mutate(ref_copy / "providers.yaml", lambda d: d["providers"].append(dict(d["providers"][0])))
    with pytest.raises(ReferenceError_, match="duplicate"):
        load_reference(ref_copy)


def test_unknown_parent_rejected(ref_copy: Path) -> None:
    def fn(d: dict) -> None:  # type: ignore[type-arg]
        d["providers"][0]["parent_provider_id"] = "nope"

    _mutate(ref_copy / "providers.yaml", fn)
    with pytest.raises(ReferenceError_, match="parent"):
        load_reference(ref_copy)


def test_bad_role_rejected(ref_copy: Path) -> None:
    def fn(d: dict) -> None:  # type: ignore[type-arg]
        d["providers"][0]["role"] = "bank"

    _mutate(ref_copy / "providers.yaml", fn)
    with pytest.raises(ReferenceError_, match="schema"):
        load_reference(ref_copy)


def test_too_few_providers_rejected(ref_copy: Path) -> None:
    _mutate(ref_copy / "providers.yaml", lambda d: d.__setitem__("providers", d["providers"][:10]))
    with pytest.raises(ReferenceError_, match="FR-DT-03"):
        load_reference(ref_copy)


def test_method_with_unknown_default_provider_rejected(ref_copy: Path) -> None:
    def fn(d: dict) -> None:  # type: ignore[type-arg]
        d["payment_methods"][0]["default_provider_id"] = "ghost"

    _mutate(ref_copy / "payment_methods.yaml", fn)
    with pytest.raises(ReferenceError_, match="default provider"):
        load_reference(ref_copy)


def test_verticals_must_be_exactly_20(ref_copy: Path) -> None:
    _mutate(ref_copy / "verticals.yaml", lambda d: d["verticals"].pop())
    with pytest.raises(ReferenceError_):
        load_reference(ref_copy)


def test_other_must_require_detail(ref_copy: Path) -> None:
    _mutate(ref_copy / "stop_reasons.yaml", lambda d: d.__setitem__("requires_detail", []))
    with pytest.raises(ReferenceError_, match="other"):
        load_reference(ref_copy)
