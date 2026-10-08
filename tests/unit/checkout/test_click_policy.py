"""Click policy (FR-CW-04): final actions in 10 languages are never allowed; hypothesis fuzzing."""

from __future__ import annotations

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from payintel.crawl.checkout.dictionary import load_dictionary
from payintel.crawl.checkout.guardrails.policy import (
    ClickPolicy,
    Control,
    Purpose,
    Verdict,
    control_from_dom,
)

D = load_dictionary()
P = ClickPolicy(D)
FINAL_PHRASES = sorted(D.phrases("final_actions"))
STEP_PHRASES = sorted(D.phrases("step_actions"))


def btn(text: str = "", **kw: object) -> Control:
    return Control(tag="button", type="submit", text=text, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text",
    [
        "Place order",
        "PLACE ORDER",
        "Pay now",
        "Buy now",
        "Zahlungspflichtig bestellen",
        "Kostenpflichtig bestellen",
        "Commander",
        "Valider la commande",
        "Bestelling plaatsen",
        "Realizar pedido",
        "Acquista ora",
        "Zamawiam i płacę",
        "Finalizar compra",
        "Slutför köp",
        "Gennemfør køb",
        "→ Place   order ←",
        "Pay with PayPal",
        "Buy with Apple Pay",
        "Complete purchase",
    ],
)
def test_final_actions_are_never_allowed(text: str) -> None:
    for purpose in Purpose:
        d = P.decide(btn(text), purpose)
        assert d.verdict == Verdict.FINAL_ACTION, (text, purpose, d)
        assert d.stop_reason == "guardrail_final_action_only"


@pytest.mark.parametrize(
    ("text", "purpose"),
    [
        ("Continue", Purpose.STEP),
        ("Continue to payment", Purpose.STEP),
        ("Weiter zur Kasse", Purpose.STEP),
        ("Next step", Purpose.STEP),
        ("Proceed to checkout", Purpose.CHECKOUT),
        ("Add to cart", Purpose.ADD_TO_CART),
        ("In den Warenkorb", Purpose.ADD_TO_CART),
        ("Checkout as guest", Purpose.GUEST),
        ("Als Gast bestellen", Purpose.GUEST),
        ("View cart", Purpose.CART),
        ("Log in", Purpose.LOGIN),
        ("Create account", Purpose.REGISTER),
    ],
)
def test_step_and_navigation_controls_are_allowed(text: str, purpose: Purpose) -> None:
    assert P.decide(btn(text), purpose).verdict == Verdict.ALLOW


def test_decoys_with_neutral_text_are_final_by_marker_or_form() -> None:
    assert (
        P.decide(btn("Continue", name="place_order"), Purpose.STEP).verdict == Verdict.FINAL_ACTION
    )
    assert (
        P.decide(btn("Continue", id="checkout-place-order"), Purpose.STEP).verdict
        == Verdict.FINAL_ACTION
    )
    assert (
        P.decide(btn("Next", classes="btn btn-pay"), Purpose.STEP).verdict == Verdict.FINAL_ACTION
    )
    assert (
        P.decide(btn("→", aria_label="Complete purchase"), Purpose.STEP).verdict
        == Verdict.FINAL_ACTION
    )
    assert (
        P.decide(Control(tag="input", type="image", alt="Pay"), Purpose.STEP).verdict
        == Verdict.FINAL_ACTION
    )
    assert (
        P.decide(btn("Continue", form_action="/?wc-ajax=checkout"), Purpose.STEP).verdict
        == Verdict.FINAL_ACTION
    )
    assert (
        P.decide(btn("Continue", form_id="confirmOrderForm"), Purpose.STEP).verdict
        == Verdict.FINAL_ACTION
    )
    assert (
        P.decide(btn("Weiter", data_attrs="action=place-order"), Purpose.STEP).verdict
        == Verdict.FINAL_ACTION
    )
    # type=button in an order form does not submit it: not final by form; unknown text → ambiguous
    d = P.decide(
        Control(tag="button", type="button", text="Mehr", form_id="checkout-payment-form"),
        Purpose.STEP,
    )
    assert d.verdict == Verdict.AMBIGUOUS
    d = P.decide(
        Control(tag="button", type="submit", text="Mehr", form_id="checkout-payment-form"),
        Purpose.STEP,
    )
    assert d.verdict == Verdict.FINAL_ACTION and d.reason == "submit of an order form"


def test_ambiguous_controls_are_not_clicked() -> None:
    assert P.decide(btn(""), Purpose.STEP).verdict == Verdict.AMBIGUOUS  # icon-only
    assert P.decide(btn("Mehr erfahren"), Purpose.STEP).verdict == Verdict.AMBIGUOUS
    assert P.decide(btn("Continue", disabled=True), Purpose.STEP).verdict == Verdict.AMBIGUOUS
    assert P.decide(btn("Add to cart"), Purpose.STEP).verdict == Verdict.AMBIGUOUS  # wrong purpose
    assert P.decide(btn("Continue"), Purpose.GUEST).verdict == Verdict.AMBIGUOUS
    d = P.decide(btn("Order"), Purpose.STEP)  # "order" alone is in no list
    assert d.verdict == Verdict.AMBIGUOUS and d.stop_reason == "guardrail_ambiguous_button"


def test_option_pickers_only_need_to_not_be_final() -> None:
    assert P.decide(
        Control(tag="input", type="radio", name="shipping_method"), Purpose.SHIPPING
    ).allowed
    assert P.decide(
        Control(tag="input", type="radio", text="Standard – 4,90 €"), Purpose.SHIPPING
    ).allowed
    assert not P.decide(Control(tag="input", type="radio", id="pay-now"), Purpose.SHIPPING).allowed


def test_control_from_dom_and_describe() -> None:
    c = control_from_dom(
        {"tag": "a", "href": "/cart", "text": "Cart (1)", "disabled": False}, selector="a.x"
    )
    assert c.tag == "a" and c.href == "/cart" and c.selector == "a.x"
    assert P.decide(c, Purpose.CART).allowed
    assert "Cart (1)" in c.describe()


# --- property test (plan §4): random final buttons in 10 languages, mutated, never allowed ---

_noise = st.sampled_from(["", " ", "  ", " ", "\t", "\n", "→", "»", "✓", "!", ".", "…", "*"])
_case = st.sampled_from(["lower", "upper", "title", "swap", "same"])
_wrap = st.sampled_from(["{}", "<b>{}</b>", "<span>{}</span> <i class=icon></i>", "  {}  "])


def _mutate(phrase: str, case: str, pre: str, post: str, wrap: str, spaces: int) -> str:
    text = phrase
    if case == "lower":
        text = text.lower()
    elif case == "upper":
        text = text.upper()
    elif case == "title":
        text = text.title()
    elif case == "swap":
        text = text.swapcase()
    if spaces:
        text = text.replace(" ", " " * (spaces + 1))
    # the DOM describer strips tags; emulate by removing them here
    text = wrap.format(text)
    text = text.replace("<b>", "").replace("</b>", "").replace("<span>", "").replace("</span>", "")
    text = text.replace("<i class=icon></i>", "")
    return pre + text + post


@settings(max_examples=3000, deadline=None)
@given(
    phrase=st.sampled_from(FINAL_PHRASES),
    case=_case,
    pre=_noise,
    post=_noise,
    wrap=_wrap,
    spaces=st.integers(min_value=0, max_value=3),
    slot=st.sampled_from(["text", "aria_label", "value", "title", "alt"]),
    purpose=st.sampled_from(list(Purpose)),
    tag=st.sampled_from(["button", "a", "input", "div"]),
)
def test_property_final_phrases_never_allowed(
    phrase: str,
    case: str,
    pre: str,
    post: str,
    wrap: str,
    spaces: int,
    slot: str,
    purpose: Purpose,
    tag: str,
) -> None:
    text = _mutate(phrase, case, pre, post, wrap, spaces)
    kwargs = {slot: text}
    control = Control(tag=tag, type="submit" if tag != "a" else "", **kwargs)  # type: ignore[arg-type]
    d = P.decide(control, purpose)
    assert d.verdict != Verdict.ALLOW, (text, slot, purpose, d)


@settings(max_examples=1500, deadline=None)
@given(
    step=st.sampled_from(STEP_PHRASES),
    final=st.sampled_from(FINAL_PHRASES),
    order=st.booleans(),
)
def test_property_mixed_step_and_final_text_never_allowed(
    step: str, final: str, order: bool
) -> None:
    text = f"{step} {final}" if order else f"{final} {step}"
    assert P.decide(btn(text), Purpose.STEP).verdict != Verdict.ALLOW


def test_random_marker_placement_never_allowed() -> None:
    rnd = random.Random(42)  # noqa: S311 - deterministic fuzzing, not cryptography
    for _ in range(2000):
        marker = rnd.choice(D.final_markers)
        attr = rnd.choice(
            [
                "name",
                "id",
                "classes",
                "data_attrs",
                "href",
                "form_action",
                "form_id",
                "form_classes",
            ]
        )
        pad = "".join(rnd.choice("abcxyz-_ ") for _ in range(rnd.randint(0, 6)))
        kwargs = {attr: f"{pad}{marker}{pad[::-1]}"}
        control = Control(tag="button", type="submit", text=rnd.choice(STEP_PHRASES), **kwargs)  # type: ignore[arg-type]
        assert P.decide(control, Purpose.STEP).verdict == Verdict.FINAL_ACTION, kwargs
