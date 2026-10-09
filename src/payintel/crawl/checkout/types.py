"""Shared value types of the checkout walk (FR-CW-09, FR-CW-13)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from payintel.core.models.base import Coverage, ScanStatus


class WalkStep(str, Enum):
    """`stop_step` of the taxonomy (5.3)."""

    NAVIGATION = "navigation"
    PROTECTION = "protection"
    PRODUCT = "product"
    CART = "cart"
    CHECKOUT = "checkout"
    REGISTRATION = "registration"
    ADDRESS = "address"
    SHIPPING = "shipping"
    PAYMENT = "payment"


# Which coverage a walk has when it stops at a given step.
COVERAGE_AT_STEP: dict[WalkStep, Coverage] = {
    WalkStep.NAVIGATION: Coverage.HOMEPAGE,
    WalkStep.PROTECTION: Coverage.HOMEPAGE,
    WalkStep.PRODUCT: Coverage.HOMEPAGE,
    WalkStep.CART: Coverage.PRODUCT,
    WalkStep.CHECKOUT: Coverage.CART,
    WalkStep.REGISTRATION: Coverage.CART,
    WalkStep.ADDRESS: Coverage.CHECKOUT,
    WalkStep.SHIPPING: Coverage.CHECKOUT,
    WalkStep.PAYMENT: Coverage.CHECKOUT,
}

# Final status (FR-CW-09) for a stop at a step, unless the reason says otherwise.
STATUS_AT_STEP: dict[WalkStep, ScanStatus] = {
    WalkStep.NAVIGATION: ScanStatus.ERROR,
    WalkStep.PROTECTION: ScanStatus.BLOCKED,
    WalkStep.PRODUCT: ScanStatus.NO_PRODUCT_FOUND,
    WalkStep.CART: ScanStatus.ADD_TO_CART_FAILED,
    WalkStep.CHECKOUT: ScanStatus.REACHED_CART,
    WalkStep.REGISTRATION: ScanStatus.LOGIN_REQUIRED,
    WalkStep.ADDRESS: ScanStatus.REACHED_CHECKOUT,
    WalkStep.SHIPPING: ScanStatus.REACHED_CHECKOUT,
    WalkStep.PAYMENT: ScanStatus.REACHED_CHECKOUT,
}
STATUS_FOR_REASON: dict[str, ScanStatus] = {
    "timeout": ScanStatus.TIMEOUT,
    "browser_crash": ScanStatus.ERROR,
    "memory_limit": ScanStatus.ERROR,
    "navigation_error": ScanStatus.ERROR,
    "http_error": ScanStatus.ERROR,
    "robots_disallowed": ScanStatus.BLOCKED,
    "email_verification_pending": ScanStatus.REGISTRATION_PENDING_VERIFICATION,
    "login_failed": ScanStatus.LOGIN_REQUIRED,
    "guest_unavailable_registration_disabled": ScanStatus.LOGIN_REQUIRED,
    "checkout_not_found": ScanStatus.REACHED_CART,
    "hosted_checkout_external": ScanStatus.REACHED_CART,  # ADR-0032: the shop has no own checkout
    "cart_empty_after_add": ScanStatus.ADD_TO_CART_FAILED,
    "min_order_value": ScanStatus.REACHED_CART,
}


@dataclass(frozen=True)
class Stop:
    step: WalkStep
    reason: str
    detail: str = ""
    page_url: str = ""
    element_selector: str = ""
    element_text: str = ""
    http_status: int = 0

    @property
    def status(self) -> ScanStatus:
        return STATUS_FOR_REASON.get(self.reason, STATUS_AT_STEP[self.step])

    @property
    def coverage(self) -> Coverage:
        return COVERAGE_AT_STEP[self.step]


class WalkStopped(Exception):
    """Raised anywhere inside the walk to end it with a taxonomy stop (FR-CW-13)."""

    def __init__(self, stop: Stop) -> None:
        super().__init__(f"{stop.step.value}/{stop.reason}: {stop.detail}")
        self.stop = stop


@dataclass
class StepTiming:
    name: str
    duration_ms: int


@dataclass
class JournalEntry:
    t_ms: int
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


class ActionJournal:
    """Walk journal (FR-CW-05, FR-CW-12, FR-CW-14): every filled field, click and refusal."""

    def __init__(self, monotonic_ms: Any) -> None:
        self._t0 = int(monotonic_ms())
        self._now = monotonic_ms
        self.entries: list[JournalEntry] = []

    def add(self, kind: str, **detail: Any) -> JournalEntry:
        e = JournalEntry(int(self._now()) - self._t0, kind, detail)
        self.entries.append(e)
        return e

    def kinds(self) -> list[str]:
        return [e.kind for e in self.entries]

    def of(self, kind: str) -> list[JournalEntry]:
        return [e for e in self.entries if e.kind == kind]

    def as_list(self) -> list[dict[str, Any]]:
        return [{"t_ms": e.t_ms, "kind": e.kind, **e.detail} for e in self.entries]


@dataclass(frozen=True)
class WalkFlags:
    """Safety flags resolved once per walk (FR-ADM-05, AS-21, AS-25)."""

    allow_shipping_step_fill: bool = True
    allow_account_registration: bool = True
    allow_payment_field_fill: bool = True
