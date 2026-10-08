"""Test values in payment fields (FR-CW-14, ADR-0003): hosted fields of known tokenizers.

Many shops reveal nothing about the acquirer until a card number is typed into
the (iframe-hosted) card field: the tokenizer then calls its API, which is
what `NetworkRecorder` observes. This module finds the hosted fields of the
documented client-side tokenizers and types the documented test values into
them through `GuardedPage.fill_payment_field`, which is the only path to a
payment field (allow-listed numbers, `allow_payment_field_fill` on). It never
submits anything: the fill is the last action before the capture phase.

Selectors come from the vendors' public integration documentation:
- Stripe Elements / Payment Element: iframes named `__privateStripeFrame*`,
  inputs `cardnumber|number`, `exp-date|expiry`, `cvc`;
- Adyen Web Drop-in / Card component: secured fields with
  `data-fieldtype="encryptedCardNumber|encryptedExpiryDate|encryptedSecurityCode"`;
- Braintree Hosted Fields: iframes `braintree-hosted-field-number|expirationDate|cvv`
  with inputs `#credit-card-number`, `#expiration`, `#cvv`;
- Checkout.com Frames: iframes `cardNumber|expiryDate|cvv` under `.card-frame`;
- Mollie Components: iframes under `#card-number|#card-expiry-date|#card-cvc`;
- Square Web Payments: `iframe.sq-card-iframe-container` / `#sq-card-number`;
- generic in-page fields by `autocomplete="cc-*"` (HTML living standard).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError

from payintel.crawl.checkout.guardrails.page import GuardedLocator, GuardedPage, PaymentField

FrameSpec = tuple[
    str, str, PaymentField
]  # (iframe selector | "" for the page, input selector, kind)


@dataclass(frozen=True)
class Tokenizer:
    name: str
    fields: tuple[FrameSpec, ...]


TOKENIZERS: tuple[Tokenizer, ...] = (
    Tokenizer(
        "stripe",
        (
            ("iframe[name^=__privateStripeFrame]", "input[name=cardnumber]", PaymentField.NUMBER),
            ("iframe[name^=__privateStripeFrame]", "input[name=exp-date]", PaymentField.EXPIRY),
            ("iframe[name^=__privateStripeFrame]", "input[name=cvc]", PaymentField.CVC),
            ("iframe[name^=__privateStripeFrame]", "input[name=number]", PaymentField.NUMBER),
            ("iframe[name^=__privateStripeFrame]", "input[name=expiry]", PaymentField.EXPIRY),
        ),
    ),
    Tokenizer(
        "adyen",
        (
            (
                "[data-cse=encryptedCardNumber] iframe",
                "input[data-fieldtype=encryptedCardNumber]",
                PaymentField.NUMBER,
            ),
            (
                "[data-cse=encryptedExpiryDate] iframe",
                "input[data-fieldtype=encryptedExpiryDate]",
                PaymentField.EXPIRY,
            ),
            (
                "[data-cse=encryptedSecurityCode] iframe",
                "input[data-fieldtype=encryptedSecurityCode]",
                PaymentField.CVC,
            ),
        ),
    ),
    Tokenizer(
        "braintree",
        (
            ("iframe#braintree-hosted-field-number", "#credit-card-number", PaymentField.NUMBER),
            ("iframe#braintree-hosted-field-expirationDate", "#expiration", PaymentField.EXPIRY),
            ("iframe#braintree-hosted-field-cvv", "#cvv", PaymentField.CVC),
        ),
    ),
    Tokenizer(
        "checkout_com",
        (
            ("iframe[id^=cardNumber], .card-number-frame iframe", "input", PaymentField.NUMBER),
            ("iframe[id^=expiryDate], .expiry-date-frame iframe", "input", PaymentField.EXPIRY),
            ("iframe[id^=cvv], .cvv-frame iframe", "input", PaymentField.CVC),
        ),
    ),
    Tokenizer(
        "mollie",
        (
            ("#card-number iframe, #cardNumber iframe", "input", PaymentField.NUMBER),
            ("#card-expiry-date iframe, #expiryDate iframe", "input", PaymentField.EXPIRY),
            ("#card-cvc iframe, #verificationCode iframe", "input", PaymentField.CVC),
        ),
    ),
    Tokenizer(
        "square",
        (
            (
                "iframe.sq-card-iframe-container, #sq-card-number iframe",
                "input",
                PaymentField.NUMBER,
            ),
            ("#sq-expiration-date iframe", "input", PaymentField.EXPIRY),
            ("#sq-cvv iframe", "input", PaymentField.CVC),
        ),
    ),
    Tokenizer(
        "generic",
        (
            ("", "input[autocomplete=cc-number]", PaymentField.NUMBER),
            ("", "input[autocomplete=cc-exp]", PaymentField.EXPIRY),
            ("", "input[autocomplete=cc-exp-month]", PaymentField.EXPIRY_MONTH),
            ("", "input[autocomplete=cc-exp-year]", PaymentField.EXPIRY_YEAR),
            ("", "input[autocomplete=cc-csc]", PaymentField.CVC),
            ("", "input[autocomplete=cc-name]", PaymentField.HOLDER),
            (
                "",
                "input[name=cardnumber], input[name=card_number], input[name=ccnumber], "
                "input[id=card-number], input[id=cardNumber]",
                PaymentField.NUMBER,
            ),
            ("", "input[name=cvc], input[name=cvv], input[name=card_cvc]", PaymentField.CVC),
        ),
    ),
)


@dataclass
class PaymentFillResult:
    tokenizer: str = ""
    filled: list[str] = field(default_factory=list)
    attempted: bool = False

    @property
    def number_filled(self) -> bool:
        return PaymentField.NUMBER.value in self.filled


async def _visible_target(
    gp: GuardedPage, frame_selector: str, input_selector: str
) -> GuardedLocator | None:
    """The first visible input for the spec, looking into every matching iframe."""
    candidates: list[GuardedLocator] = []
    if frame_selector:
        candidates.extend(f.locator(input_selector) for f in await gp.frames(frame_selector))
    else:
        candidates.append(gp.locator(input_selector))
    for target in candidates:
        try:
            if await target.first.is_visible():
                return target.first
        except PlaywrightError as exc:  # detached frame / cross-origin: "not found"
            gp.journal.add("payment_probe_error", selector=target.selector, error=str(exc)[:120])
    return None


async def detect_tokenizer(gp: GuardedPage) -> str:
    """Name of the first tokenizer whose card-number field is on the page, or ''."""
    for tok in TOKENIZERS:
        for frame_sel, input_sel, kind in tok.fields:
            if kind != PaymentField.NUMBER:
                continue
            if await _visible_target(gp, frame_sel, input_sel) is not None:
                return tok.name
    return ""


async def fill_test_card(gp: GuardedPage) -> PaymentFillResult:
    """Type the documented test values into the hosted fields found on the page.

    Returns which fields were filled. The number is always the first preferred
    documented number; nothing is submitted. Refused fills (flag off) show up in
    the walk journal as `payment_fill_refused`.
    """
    result = PaymentFillResult()
    name = await detect_tokenizer(gp)
    if not name:
        return result
    result.tokenizer = name
    result.attempted = True
    tok = next(t for t in TOKENIZERS if t.name == name)
    number = gp.payment_values.preferred[0]
    done: set[PaymentField] = set()
    for frame_sel, input_sel, kind in tok.fields:
        if kind in done:
            continue
        target = await _visible_target(gp, frame_sel, input_sel)
        if target is None:
            continue
        try:
            ok = await gp.fill_payment_field(target, kind, number=number)
        except PlaywrightError:
            ok = False
        if ok:
            done.add(kind)
            result.filled.append(kind.value)
    gp.journal.add(
        "payment_fill_summary", tokenizer=name, filled=list(result.filled), number_last4=number[-4:]
    )
    return result
