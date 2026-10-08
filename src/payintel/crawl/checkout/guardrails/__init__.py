"""Guardrail layer of the checkout scanner (FR-CW-04): every browser action passes through it.

Adapters and the heuristic never see a Playwright `Page`; they get a
`GuardedPage` whose only mutating methods are `goto`, `click`, `fill_buyer_field`,
`fill_credential`, `fill_payment_field`, `check_required_checkbox`,
`select_first_option`, `select_shipping` and `select_country`. There is no
method for solving a CAPTCHA, logging into a foreign account, ticking a
newsletter box or changing the IP address.
"""

from payintel.crawl.checkout.guardrails.page import (
    BuyerField,
    GuardedFrame,
    GuardedLocator,
    GuardedPage,
    GuardrailStop,
    PaymentField,
)
from payintel.crawl.checkout.guardrails.policy import (
    ClickPolicy,
    Control,
    Decision,
    Purpose,
    Verdict,
)

__all__ = [
    "BuyerField",
    "ClickPolicy",
    "Control",
    "Decision",
    "GuardedFrame",
    "GuardedLocator",
    "GuardedPage",
    "GuardrailStop",
    "PaymentField",
    "Purpose",
    "Verdict",
]
