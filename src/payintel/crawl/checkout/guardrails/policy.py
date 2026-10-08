"""Click policy (FR-CW-04, AS-20, AS-21): decides for every control whether the walk may press it.

A `Control` is what the DOM says about an element: visible text, `aria-label`,
`value`, `title`, `alt`, `name`, `id`, classes, `type`, data-* values, `href`
and the enclosing form. The decision ladder:

1. any label matches a final-action phrase or a wallet brand        → FINAL_ACTION
2. any attribute carries a final marker (`place_order`, `pay-now`…)  → FINAL_ACTION
3. `type=submit` inside a form that looks like an order/payment form → FINAL_ACTION
4. label matches the vocabulary of the requested purpose (step, add
   to cart, cart/checkout navigation, guest, login, register)        → ALLOW
5. otherwise (no readable label, unknown text, mixed signals)        → AMBIGUOUS

FINAL_ACTION and AMBIGUOUS are both "do not click"; they differ only in the
stop code written to `obs_scan_stop` (`guardrail_final_action_only` vs
`guardrail_ambiguous_button`). The policy is pure and browser-free so the
property test can throw millions of button variants at it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from payintel.crawl.checkout.dictionary import CheckoutDictionary, normalize


class Purpose(str, Enum):
    """What the caller wants to do; the policy checks the control against it."""

    STEP = "step"  # continue / next / proceed between checkout steps
    ADD_TO_CART = "add_to_cart"
    CART = "cart"  # go to the cart page
    CHECKOUT = "checkout"  # go to the checkout page (not placing the order)
    GUEST = "guest"  # choose guest checkout
    LOGIN = "login"
    REGISTER = "register"  # submit the registration form (FR-CW-12 only)
    SHIPPING = "shipping"  # pick a shipping option
    VARIANT = "variant"  # pick a product variant / option
    DISMISS = "dismiss"  # close cookie banner / popup


class Verdict(str, Enum):
    ALLOW = "allow"
    FINAL_ACTION = "final_action"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Control:
    tag: str = ""
    type: str = ""
    role: str = ""
    text: str = ""
    aria_label: str = ""
    value: str = ""
    title: str = ""
    alt: str = ""
    name: str = ""
    id: str = ""
    classes: str = ""
    data_attrs: str = ""  # "key=value key2=value2"
    href: str = ""
    form_action: str = ""
    form_id: str = ""
    form_classes: str = ""
    form_name: str = ""
    disabled: bool = False
    selector: str = ""

    def labels(self) -> list[str]:
        return [v for v in (self.text, self.aria_label, self.value, self.title, self.alt) if v]

    def attributes(self) -> list[str]:
        return [
            v
            for v in (
                self.name,
                self.id,
                self.classes,
                self.data_attrs,
                self.href,
                self.form_action,
                self.form_id,
                self.form_classes,
                self.form_name,
            )
            if v
        ]

    def describe(self) -> str:
        label = (self.text or self.aria_label or self.value or self.title or self.alt)[:120]
        return f"<{self.tag or '?'} {self.type or ''} {label!r} id={self.id!r} name={self.name!r}>"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    matched: str = ""
    control: Control = field(default_factory=Control)

    @property
    def allowed(self) -> bool:
        return self.verdict == Verdict.ALLOW

    @property
    def stop_reason(self) -> str:
        return (
            "guardrail_final_action_only"
            if self.verdict == Verdict.FINAL_ACTION
            else "guardrail_ambiguous_button"
        )


_PURPOSE_SECTIONS: dict[Purpose, tuple[str, ...]] = {
    Purpose.STEP: ("step_actions",),
    Purpose.ADD_TO_CART: ("add_to_cart",),
    Purpose.CART: ("cart_words", "step_actions"),
    Purpose.CHECKOUT: ("checkout_words", "step_actions"),
    Purpose.GUEST: ("guest_checkout",),
    Purpose.LOGIN: ("login",),
    Purpose.REGISTER: ("register",),
}
# Attribute fragments that identify a control for a purpose when it has no readable label.
_PURPOSE_MARKERS: dict[Purpose, tuple[str, ...]] = {
    Purpose.ADD_TO_CART: ("add-to-cart", "addtocart", "add_to_cart", "product-add", "tocart"),
    Purpose.CART: (
        "/cart",
        "/basket",
        "/warenkorb",
        "/panier",
        "/winkelwagen",
        "/carrito",
        "/carrello",
        "/koszyk",
        "/carrinho",
        "/varukorg",
        "/kurv",
        "minicart",
    ),
    Purpose.CHECKOUT: (
        "/checkout",
        "/kasse",
        "/caisse",
        "/afrekenen",
        "/order",
        "/onepage",
        "/commande",
        "/zamowienie",
        "/kassa",
        "/kassen",
        "proceed-to-checkout",
        "checkout-button",
    ),
    Purpose.GUEST: ("guest", "gast", "invite", "ospite", "gosc", "convidado", "gaest"),
    Purpose.LOGIN: ("login", "signin", "sign-in", "anmelden"),
    Purpose.REGISTER: ("register", "signup", "sign-up", "create-account", "registrieren"),
    Purpose.DISMISS: ("accept", "close", "dismiss", "consent", "cookie", "agree", "ok"),
}
# A step button may carry a step phrase in the label and still submit an order form;
# "contains checkout" alone is a navigation hint, not an order marker.


class ClickPolicy:
    def __init__(self, dictionary: CheckoutDictionary) -> None:
        self.d = dictionary

    # --- the three "never" checks ---------------------------------------------------
    def final_phrase(self, control: Control) -> str | None:
        for label in control.labels():
            hit = self.d.match(label, "final_actions")
            if hit:
                return hit
            norm = normalize(label)
            for brand in self.d.final_brands:
                if f" {brand} " in f" {norm} ":
                    return f"brand:{brand}"
        return None

    def final_marker(self, control: Control) -> str | None:
        for attr in control.attributes():
            hit = self.d.marker_in(attr, self.d.final_markers)
            if hit:
                return hit
        return None

    def order_form_submit(self, control: Control) -> str | None:
        if control.type.lower() not in {"submit", "image"} and control.tag.lower() != "button":
            return None
        if control.tag.lower() == "button" and control.type.lower() in {"button", "reset"}:
            return None
        for attr in (control.form_action, control.form_id, control.form_classes, control.form_name):
            hit = self.d.marker_in(attr, self.d.order_form_markers)
            if hit:
                return hit
        return None

    # --- purpose check -----------------------------------------------------------------
    def purpose_match(self, control: Control, purpose: Purpose) -> str | None:
        if purpose in {Purpose.SHIPPING, Purpose.VARIANT}:
            # option pickers: radios, selects, swatches — never carry order semantics;
            # the "never" checks above still apply to them.
            return f"{purpose.value}:option"
        for section in _PURPOSE_SECTIONS.get(purpose, ()):
            for label in control.labels():
                hit = self.d.match(label, section)
                if hit:
                    return hit
        for marker in _PURPOSE_MARKERS.get(purpose, ()):
            for attr in control.attributes():
                if marker in attr.lower():
                    return f"marker:{marker}"
        return None

    def exact_purpose_phrase(self, control: Control, purpose: Purpose) -> str | None:
        """The whole label *is* a dictionary phrase of the purpose (e.g. «Als Gast bestellen»).

        Such a label may contain a final word («bestellen») and is still the
        choice of guest checkout, not the order button. The exemption applies to
        the label only: markers and order-form submits stay final.
        """
        for section in _PURPOSE_SECTIONS.get(purpose, ()):
            for label in control.labels():
                norm = normalize(label)
                if norm and norm in self.d.sections[section]:
                    return norm
        return None

    def decide(self, control: Control, purpose: Purpose) -> Decision:
        marker = self.final_marker(control)
        if marker:
            return Decision(Verdict.FINAL_ACTION, "final-action marker", marker, control)
        order_form = self.order_form_submit(control)
        if order_form:
            return Decision(Verdict.FINAL_ACTION, "submit of an order form", order_form, control)
        final = self.final_phrase(control)
        if final:
            exact = self.exact_purpose_phrase(control, purpose)
            if exact is None or any(
                normalize(label) in self.d.sections["final_actions"] for label in control.labels()
            ):
                return Decision(Verdict.FINAL_ACTION, "final-action phrase", final, control)
        if control.disabled:
            return Decision(Verdict.AMBIGUOUS, "disabled control", "", control)
        if purpose == Purpose.STEP and any(
            self.d.match(label, "final_actions") for label in control.labels()
        ):  # pragma: no cover - unreachable after the final_phrase check, kept for clarity
            return Decision(Verdict.AMBIGUOUS, "step label mixed with final phrase", "", control)
        hit = self.purpose_match(control, purpose)
        if hit is None:
            why = "no readable label" if not control.labels() else f"label not a {purpose.value}"
            return Decision(Verdict.AMBIGUOUS, why, "", control)
        return Decision(Verdict.ALLOW, f"{purpose.value} control", hit, control)


def control_from_dom(d: dict[str, object], selector: str = "") -> Control:
    """Build a `Control` from the JSON produced by `DESCRIBE_CONTROL_JS`."""

    def s(key: str) -> str:
        v = d.get(key)
        return str(v)[:500] if v is not None else ""

    return Control(
        tag=s("tag"),
        type=s("type"),
        role=s("role"),
        text=s("text"),
        aria_label=s("ariaLabel"),
        value=s("value"),
        title=s("title"),
        alt=s("alt"),
        name=s("name"),
        id=s("id"),
        classes=s("classes"),
        data_attrs=s("data"),
        href=s("href"),
        form_action=s("formAction"),
        form_id=s("formId"),
        form_classes=s("formClasses"),
        form_name=s("formName"),
        disabled=bool(d.get("disabled")),
        selector=selector or s("selector"),
    )
