"""`GuardedPage`: the only handle adapters get on the browser (FR-CW-04, FR-CW-05, FR-CW-14).

Every mutating call is checked and journaled:

- `click(target, purpose)` runs the `ClickPolicy`; a FINAL_ACTION / AMBIGUOUS
  verdict raises `GuardrailStop` and nothing is pressed;
- `fill_buyer_field` can only type values of the synthetic `Identity`
  (the caller names the field kind, not the text);
- `fill_payment_field` can only type values from `TestPaymentValues` and only
  while `allow_payment_field_fill` is on;
- `check_required_checkbox` refuses newsletter / marketing / account-creation
  boxes and optional consents;
- `goto` asks the walk's URL policy (robots.txt, egress) before navigating.

`GuardedLocator` wraps a Playwright locator with read-only accessors so that
adapters can look at elements but cannot act on them directly.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, FrameLocator, Locator, Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from payintel.crawl.checkout.dictionary import CheckoutDictionary, normalize
from payintel.crawl.checkout.guardrails.injection import (
    CLICKABLE_SELECTOR,
    DESCRIBE_CONTROL_JS,
    PHASE_CAPTURE,
)
from payintel.crawl.checkout.guardrails.policy import (
    ClickPolicy,
    Control,
    Decision,
    Purpose,
    control_from_dom,
)
from payintel.crawl.checkout.identities import Identity
from payintel.crawl.checkout.payment_values import TestPaymentValues
from payintel.crawl.checkout.types import ActionJournal, Stop, WalkFlags, WalkStep, WalkStopped

_LABEL_JS = r"""
(el) => {
  const a = (n) => (el.getAttribute && el.getAttribute(n)) || "";
  let label = "";
  if (el.labels && el.labels.length) label = Array.from(el.labels).map((l) => l.innerText || l.textContent || "").join(" ");
  if (!label) label = a("aria-label");
  if (!label && a("aria-labelledby")) { const t = document.getElementById(a("aria-labelledby")); if (t) label = t.innerText || ""; }
  if (!label) { const p = el.closest("label"); if (p) label = p.innerText || p.textContent || ""; }
  if (!label && el.parentElement) label = (el.parentElement.innerText || "").slice(0, 300);
  return { label: (label || "").trim().slice(0, 300), required: !!(el.required || a("aria-required") === "true"),
           checked: !!el.checked, name: a("name"), id: el.id || "", type: a("type") };
}
"""
_OPTIONS_JS = r"""
(el) => Array.from(el.options || []).map((o) => ({ value: o.value, text: (o.text || "").trim(), disabled: !!o.disabled }))
"""
_PLACEHOLDER_RE = re.compile(
    r"^(-+|—|\.{3}|…|\s*)$|^(please\s+)?(choose|select|pick|wählen|bitte|auswählen|choisir|"
    r"sélectionner|kies|selecteer|elige|seleccion|scegli|seleziona|wybierz|escolh|selecion|"
    r"välj|vælg)\b",
    re.I,
)


class BuyerField(str, Enum):
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    FULL_NAME = "full_name"
    EMAIL = "email"
    EMAIL_CONFIRM = "email_confirm"
    PHONE = "phone"
    STREET = "street"
    HOUSE_NUMBER = "house_number"
    STREET_LINE = "street_line"
    POSTCODE = "postcode"
    CITY = "city"
    REGION = "region"


class PaymentField(str, Enum):
    NUMBER = "number"
    EXPIRY = "expiry"  # MM/YY in one field
    EXPIRY_MONTH = "expiry_month"
    EXPIRY_YEAR = "expiry_year"  # YY
    EXPIRY_YEAR_FULL = "expiry_year_full"  # YYYY
    CVC = "cvc"
    HOLDER = "holder"


class GuardrailStop(WalkStopped):
    """A click was refused by the policy; the walk ends with a guardrail stop code."""

    def __init__(self, decision: Decision, page_url: str) -> None:
        self.decision = decision
        c = decision.control
        super().__init__(
            Stop(
                step=WalkStep.PAYMENT,
                reason=decision.stop_reason,
                detail=f"{decision.reason}: {decision.matched or c.describe()}"[:500],
                page_url=page_url,
                element_selector=c.selector[:255],
                element_text=(c.text or c.aria_label or c.value)[:255],
            )
        )


UrlCheck = Callable[[str], str | None]  # returns a refusal reason or None


class GuardedLocator:
    """Read-only view of a Playwright locator; actions go through `GuardedPage`."""

    def __init__(self, locator: Locator, selector: str) -> None:
        self._locator = locator
        self.selector = selector

    @property
    def first(self) -> GuardedLocator:
        return GuardedLocator(self._locator.first, self.selector)

    def nth(self, index: int) -> GuardedLocator:
        return GuardedLocator(self._locator.nth(index), f"{self.selector}:nth({index})")

    def locator(self, selector: str) -> GuardedLocator:
        return GuardedLocator(self._locator.locator(selector), f"{self.selector} {selector}")

    async def count(self) -> int:
        try:
            return await self._locator.count()
        except PlaywrightError:
            return 0

    async def is_visible(self) -> bool:
        try:
            return await self._locator.first.is_visible()
        except PlaywrightError:
            return False

    async def text(self) -> str:
        try:
            return (await self._locator.first.inner_text(timeout=2_000)).strip()
        except PlaywrightError:
            return ""

    async def attr(self, name: str) -> str:
        try:
            return (await self._locator.first.get_attribute(name, timeout=2_000)) or ""
        except PlaywrightError:
            return ""

    async def is_checked(self) -> bool:
        try:
            return await self._locator.first.is_checked(timeout=2_000)
        except PlaywrightError:
            return False

    async def describe(self) -> Control:
        d = await self._locator.first.evaluate(DESCRIBE_CONTROL_JS)
        return control_from_dom(d, selector=self.selector)


class GuardedFrame:
    def __init__(self, frame: FrameLocator | Frame, selector: str) -> None:
        self._frame = frame
        self.selector = selector

    def locator(self, selector: str) -> GuardedLocator:
        return GuardedLocator(self._frame.locator(selector), f"{self.selector} >> {selector}")


@dataclass
class GuardedPage:
    page: Page
    policy: ClickPolicy
    dictionary: CheckoutDictionary
    identity: Identity
    payment_values: TestPaymentValues
    flags: WalkFlags
    journal: ActionJournal
    url_check: UrlCheck
    action_timeout_ms: int = 10_000
    max_clicks: int = 40
    current_year: int = 2026

    def __post_init__(self) -> None:
        self._clicks = 0
        self._phase = "walk"

    # --- read side ----------------------------------------------------------------------
    @property
    def url(self) -> str:
        return self.page.url

    @property
    def clicks(self) -> int:
        return self._clicks

    def locator(self, selector: str) -> GuardedLocator:
        return GuardedLocator(self.page.locator(selector), selector)

    def by_text(self, text: str, *, exact: bool = False) -> GuardedLocator:
        return GuardedLocator(self.page.get_by_text(text, exact=exact), f"text={text!r}")

    def frame(self, selector: str) -> GuardedFrame:
        return GuardedFrame(self.page.frame_locator(selector), selector)

    async def frames(self, selector: str) -> list[GuardedFrame]:
        """One `GuardedFrame` per iframe matching the selector (hosted fields use several)."""
        try:
            n = await self.page.locator(selector).count()
        except PlaywrightError:
            return []
        return [
            GuardedFrame(self.page.frame_locator(selector).nth(i), f"{selector}:nth({i})")
            for i in range(min(n, 12))
        ]

    def clickables(self) -> GuardedLocator:
        return self.locator(CLICKABLE_SELECTOR)

    async def title(self) -> str:
        try:
            return await self.page.title()
        except PlaywrightError:
            return ""

    async def body_text(self, limit: int = 20_000) -> str:
        try:
            txt: str = await self.page.evaluate(
                "() => (document.body && document.body.innerText) || ''"
            )
            return txt[:limit]
        except PlaywrightError:
            return ""

    async def content(self) -> str:
        try:
            return await self.page.content()
        except PlaywrightError:
            return ""

    async def find_controls(
        self, section: str, *, limit: int = 200
    ) -> list[tuple[GuardedLocator, Control]]:
        """Visible clickable controls whose label matches a dictionary section."""
        out: list[tuple[GuardedLocator, Control]] = []
        loc = self.page.locator(CLICKABLE_SELECTOR)
        try:
            n = min(await loc.count(), limit)
        except PlaywrightError:
            return out
        for i in range(n):
            el = loc.nth(i)
            try:
                if not await el.is_visible():
                    continue
                d = await el.evaluate(DESCRIBE_CONTROL_JS)
            except PlaywrightError:
                continue
            control = control_from_dom(d, selector=f"{CLICKABLE_SELECTOR} >> nth={i}")
            if any(self.dictionary.match(label, section) for label in control.labels()):
                out.append((GuardedLocator(el, control.selector), control))
        return out

    async def wait_idle(self, timeout_ms: int) -> bool:
        """Network idle or timeout (FR-CW-02); never raises."""
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
            return True
        except PlaywrightError:
            return False

    async def wait_for(self, selector: str, timeout_ms: int | None = None) -> bool:
        try:
            await self.page.locator(selector).first.wait_for(
                state="visible", timeout=timeout_ms or self.action_timeout_ms
            )
            return True
        except PlaywrightError:
            return False

    # --- phase -------------------------------------------------------------------------
    async def enter_capture_phase(self) -> None:
        """From here on the browser-side guard cancels every form submission."""
        self._phase = PHASE_CAPTURE
        for frame in self.page.frames:
            try:
                await frame.evaluate(
                    "() => { if (window.__payintel) window.__payintel.phase = 'capture'; }"
                )
            except PlaywrightError:
                continue
        self.journal.add("phase", phase=PHASE_CAPTURE)

    async def dom_blocked(self) -> list[dict[str, Any]]:
        """Attempts cancelled by the injected guard (journaled as `dom_blocked`)."""
        out: list[dict[str, Any]] = []
        for frame in self.page.frames:
            try:
                items = await frame.evaluate(
                    "() => (window.__payintel && window.__payintel.blocked) || []"
                )
            except PlaywrightError:
                continue
            for it in items:
                out.append(dict(it))
        for it in out:
            self.journal.add(
                "dom_blocked",
                attempt=it.get("kind"),
                why=it.get("why"),
                tag=it.get("tag"),
                text=it.get("text"),
            )
        return out

    # --- mutating side ----------------------------------------------------------------
    async def goto(self, url: str, *, step: WalkStep = WalkStep.NAVIGATION) -> Response | None:
        refusal = self.url_check(url)
        if refusal is not None:
            self.journal.add("goto_refused", url=url, reason=refusal)
            raise WalkStopped(Stop(step, refusal, f"navigation refused: {url}", page_url=url))
        self.journal.add("goto", url=url)
        try:
            return await self.page.goto(
                url, wait_until="domcontentloaded", timeout=self.action_timeout_ms * 3
            )
        except PlaywrightTimeoutError as exc:
            raise WalkStopped(
                Stop(step, "timeout", f"navigation timeout: {url}", page_url=url)
            ) from exc
        except PlaywrightError as exc:
            raise WalkStopped(
                Stop(step, "navigation_error", str(exc).splitlines()[0][:300], page_url=url)
            ) from exc

    async def click(
        self, target: GuardedLocator, purpose: Purpose, *, timeout_ms: int | None = None
    ) -> Decision:
        if self._phase == PHASE_CAPTURE:
            raise WalkStopped(
                Stop(
                    WalkStep.PAYMENT, "other", "click requested in capture phase", page_url=self.url
                )
            )
        self._clicks += 1
        if self._clicks > self.max_clicks:
            raise WalkStopped(
                Stop(
                    WalkStep.CHECKOUT,
                    "other",
                    f"click budget of {self.max_clicks} exhausted",
                    page_url=self.url,
                )
            )
        control = await target.describe()
        decision = self.policy.decide(control, purpose)
        if not decision.allowed:
            self.journal.add(
                "click_refused",
                purpose=purpose.value,
                verdict=decision.verdict.value,
                reason=decision.reason,
                matched=decision.matched,
                control=control.describe(),
            )
            raise GuardrailStop(decision, self.url)
        self.journal.add(
            "click", purpose=purpose.value, matched=decision.matched, control=control.describe()
        )
        await target._locator.first.click(timeout=timeout_ms or self.action_timeout_ms)
        return decision

    def _buyer_value(self, kind: BuyerField) -> str:
        i = self.identity
        return {
            BuyerField.FIRST_NAME: i.first_name,
            BuyerField.LAST_NAME: i.last_name,
            BuyerField.FULL_NAME: i.full_name,
            BuyerField.EMAIL: i.email,
            BuyerField.EMAIL_CONFIRM: i.email,
            BuyerField.PHONE: i.phone,
            BuyerField.STREET: i.street,
            BuyerField.HOUSE_NUMBER: i.house_number,
            BuyerField.STREET_LINE: i.street_line,
            BuyerField.POSTCODE: i.postcode,
            BuyerField.CITY: i.city,
            BuyerField.REGION: i.region,
        }[kind]

    async def fill_buyer_field(self, target: GuardedLocator, kind: BuyerField) -> bool:
        """Type one synthetic identity value (FR-CW-05). Returns False when the field is absent."""
        value = self._buyer_value(kind)
        loc = target._locator.first
        try:
            if not await loc.is_visible():
                return False
            await loc.fill(value, timeout=self.action_timeout_ms)
        except PlaywrightError:
            return False
        self.journal.add("fill", field=kind.value, selector=target.selector, value=value)
        return True

    async def fill_credential(self, target: GuardedLocator, kind: str, value: str) -> bool:
        """Account e-mail / password for FR-CW-12 only (gated by the registration flag)."""
        if not self.flags.allow_account_registration:
            self.journal.add("fill_refused", field=kind, reason="allow_account_registration=false")
            return False
        if kind == "email" and value != self.identity.email:
            self.journal.add(
                "fill_refused", field=kind, reason="account e-mail must be the probe address"
            )
            return False
        loc = target._locator.first
        try:
            if not await loc.is_visible():
                return False
            await loc.fill(value, timeout=self.action_timeout_ms)
        except PlaywrightError:
            return False
        self.journal.add(
            "fill",
            field=f"account_{kind}",
            selector=target.selector,
            value="***" if kind == "password" else value,
        )
        return True

    def _payment_value(self, kind: PaymentField, number: str) -> str:
        mm, yy, yyyy = self.payment_values.expiry(self.current_year)
        return {
            PaymentField.NUMBER: number,
            PaymentField.EXPIRY: f"{mm}/{yy}",
            PaymentField.EXPIRY_MONTH: mm,
            PaymentField.EXPIRY_YEAR: yy,
            PaymentField.EXPIRY_YEAR_FULL: yyyy,
            PaymentField.CVC: self.payment_values.cvc_for(number),
            PaymentField.HOLDER: self.payment_values.holder_name,
        }[kind]

    async def fill_payment_field(
        self, target: GuardedLocator, kind: PaymentField, *, number: str | None = None
    ) -> bool:
        """Type a test value into a payment field (FR-CW-14, ADR-0003).

        The number must be byte-for-byte in `reference/test_payment_values.yaml`;
        the flag `allow_payment_field_fill` must be on. Nothing else ever reaches
        a payment field.
        """
        if not self.flags.allow_payment_field_fill:
            self.journal.add(
                "payment_fill_refused", field=kind.value, reason="allow_payment_field_fill=false"
            )
            return False
        number = number or self.payment_values.preferred[0]
        if not self.payment_values.allowed(number):
            self.journal.add(
                "payment_fill_refused", field=kind.value, reason="value not in reference"
            )
            raise WalkStopped(
                Stop(
                    WalkStep.PAYMENT,
                    "other",
                    "payment value outside reference list",
                    page_url=self.url,
                )
            )
        value = self._payment_value(kind, number)
        loc = target._locator.first
        try:
            if not await loc.is_visible():
                return False
            await loc.fill(value, timeout=self.action_timeout_ms)
        except PlaywrightError:
            return False
        self.journal.add("payment_fill", field=kind.value, selector=target.selector, value=value)
        return True

    async def check_required_checkbox(self, target: GuardedLocator) -> bool:
        """Tick a checkbox only when it is required and not a newsletter/marketing consent."""
        loc = target._locator.first
        try:
            info = await loc.evaluate(_LABEL_JS)
        except PlaywrightError:
            return False
        label = str(info.get("label", ""))
        forbidden = self.dictionary.match(label, "forbidden_consents")
        for attr in (str(info.get("name", "")), str(info.get("id", ""))):
            if any(
                m in attr.lower()
                for m in (
                    "newsletter",
                    "marketing",
                    "subscribe",
                    "create_account",
                    "createaccount",
                    "register",
                )
            ):
                forbidden = forbidden or f"attr:{attr}"
        if forbidden:
            self.journal.add(
                "checkbox_skipped", label=label[:120], reason=f"forbidden consent: {forbidden}"
            )
            return False
        required = bool(info.get("required"))
        if not required and not self.dictionary.match(label, "required_consents"):
            self.journal.add("checkbox_skipped", label=label[:120], reason="optional")
            return False
        if info.get("checked"):
            return True
        try:
            await loc.check(timeout=self.action_timeout_ms)
        except PlaywrightError:
            return False
        self.journal.add("checkbox", label=label[:120], required=required)
        return True

    async def select_first_option(self, target: GuardedLocator) -> str | None:
        """Pick the first real option of a `<select>` (variants, FR-CW-02)."""
        loc = target._locator.first
        try:
            options = await loc.evaluate(_OPTIONS_JS)
        except PlaywrightError:
            return None
        for o in options:
            value, text = str(o.get("value", "")), str(o.get("text", ""))
            if (
                o.get("disabled")
                or not value
                or _PLACEHOLDER_RE.match(text)
                or _PLACEHOLDER_RE.match(value)
            ):
                continue
            try:
                await loc.select_option(value=value, timeout=self.action_timeout_ms)
            except PlaywrightError:
                return None
            self.journal.add("select", selector=target.selector, value=value, text=text[:80])
            return value
        return None

    async def select_country(self, target: GuardedLocator) -> bool:
        loc = target._locator.first
        cc = self.identity.country
        try:
            options = await loc.evaluate(_OPTIONS_JS)
        except PlaywrightError:
            return False
        wanted = None
        for o in options:
            value, text = str(o.get("value", "")), str(o.get("text", ""))
            if value.upper() == cc or normalize(text) == normalize(_COUNTRY_NAMES.get(cc, "")):
                wanted = value
                break
        if wanted is None:
            return False
        try:
            await loc.select_option(value=wanted, timeout=self.action_timeout_ms)
        except PlaywrightError:
            return False
        self.journal.add("select", selector=target.selector, value=wanted, field="country")
        return True

    async def select_shipping(self, target: GuardedLocator) -> Decision:
        """Choose a shipping option (radio / button); still subject to the click policy."""
        control = await target.describe()
        decision = self.policy.decide(control, Purpose.SHIPPING)
        if not decision.allowed:
            self.journal.add(
                "click_refused",
                purpose="shipping",
                verdict=decision.verdict.value,
                control=control.describe(),
            )
            raise GuardrailStop(decision, self.url)
        loc = target._locator.first
        if control.type in {"radio", "checkbox"}:
            await loc.check(timeout=self.action_timeout_ms)
        else:
            self._clicks += 1
            await loc.click(timeout=self.action_timeout_ms)
        self.journal.add("shipping", control=control.describe())
        return decision


_COUNTRY_NAMES: dict[str, str] = {
    "DE": "Deutschland",
    "AT": "Österreich",
    "CH": "Schweiz",
    "GB": "United Kingdom",
    "IE": "Ireland",
    "US": "United States",
    "CA": "Canada",
    "AU": "Australia",
    "FR": "France",
    "BE": "België",
    "NL": "Nederland",
    "ES": "España",
    "IT": "Italia",
    "PL": "Polska",
    "PT": "Portugal",
    "SE": "Sverige",
    "DK": "Danmark",
    "NO": "Norge",
    "FI": "Suomi",
    "CZ": "Česko",
}
