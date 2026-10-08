"""The checkout walk (FR-CW-02 … FR-CW-14): homepage → product → cart → checkout → payment step.

One `CheckoutWalker.walk()` is one domain in one isolated browser context.
The sequence is fixed; every step is timed; the walk ends either at the
payment step (observations captured, nothing submitted) or with a `Stop`
from the taxonomy plus a screenshot and the DOM (FR-CW-13). Everything that
touches the page goes through `GuardedPage`, so the final-action rules of
FR-CW-04 hold regardless of what an adapter or the heuristic selects.

Decisions made here (documented in ADR-0014):
- a product is any link the adapter/heuristic selectors find that leads to a
  page with an add-to-cart control; up to three candidates are tried;
- the login wall is resolved in this order: address fields already visible
  (guest/one-page) → guest control → existing account login → registration
  (FR-CW-12, flag) → `guest_unavailable_registration_disabled`;
- payment test values are typed only when the payment step revealed no
  method on its own and a documented tokenizer is present (FR-CW-14);
- a walk deadline hit is a `navigation/timeout` stop whose coverage reflects
  the furthest step started (ScanRun.coverage is not from the stop's step).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from payintel.core.models.base import Coverage, ScanStatus, StoreAccountStatus
from payintel.crawl.checkout.accounts import Credentials
from payintel.crawl.checkout.adapters import AdapterHints, adapter_for
from payintel.crawl.checkout.blocking import detect_block, has_captcha_widget
from payintel.crawl.checkout.browser import BrowserPool, ContextOptions, WalkContext
from payintel.crawl.checkout.capture import (
    CheckoutCapture,
    NetworkRecorder,
    api_favicon_fetcher,
    capture_page,
    screenshot_jpeg,
)
from payintel.crawl.checkout.dictionary import CheckoutDictionary
from payintel.crawl.checkout.guardrails.page import (
    BuyerField,
    GuardedLocator,
    GuardedPage,
    UrlCheck,
)
from payintel.crawl.checkout.guardrails.policy import ClickPolicy, Purpose
from payintel.crawl.checkout.identities import Identity
from payintel.crawl.checkout.payment_fill import PaymentFillResult, detect_tokenizer, fill_test_card
from payintel.crawl.checkout.payment_values import TestPaymentValues
from payintel.crawl.checkout.stops import stop_from_exception
from payintel.crawl.checkout.types import (
    COVERAGE_AT_STEP,
    ActionJournal,
    StepTiming,
    Stop,
    WalkFlags,
    WalkStep,
    WalkStopped,
)
from payintel.crawl.sanitize import sanitize_text

EVIDENCE_TIMEOUT_S = 3.0
_COVERAGE_ORDER = [
    Coverage.HOMEPAGE,
    Coverage.PRODUCT,
    Coverage.CART,
    Coverage.CHECKOUT,
    Coverage.PAYMENT_STEP,
]
_NON_PRODUCT_PATH_RE = re.compile(
    r"/(cart|checkout|basket|warenkorb|kasse|panier|caisse|account|login|register|wishlist|"
    r"search|blog|news|contact|about|impressum|datenschutz|privacy|terms|agb|faq|hilfe|help|"
    r"category|categories|collections?|kategorie)(/|$|\?)",
    re.I,
)
_VISIBLE_REQUIRED_JS = r"""
() => Array.from(document.querySelectorAll("input[required], select[required], textarea[required]"))
  .filter((el) => {
    const r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return false;
    const t = (el.getAttribute("type") || "").toLowerCase();
    if (["hidden", "checkbox", "radio", "submit", "button"].includes(t)) return false;
    return !(el.value || "").trim();
  })
  .map((el) => (el.getAttribute("name") || el.id || el.getAttribute("autocomplete") || el.tagName.toLowerCase()).slice(0, 60))
  .slice(0, 20)
"""
_VISIBLE_COUNT_JS = r"""
(selectors) => {
  let n = 0;
  for (const sel of selectors) {
    let els = [];
    try { els = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    for (const el of els) {
      const r = el.getBoundingClientRect();
      const visible = r.width > 0 && r.height > 0;
      const t = (el.getAttribute("type") || "").toLowerCase();
      // hidden radio inputs behind styled labels still count when their label is visible
      if (visible || (t === "radio" && el.labels && el.labels.length)) n += 1;
    }
  }
  return n;
}
"""
_METHOD_COUNT_JS = r"""
([blocks, options]) => {
  const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const seen = new Set();
  let n = 0;
  const countIn = (root) => {
    for (const el of root.querySelectorAll("input[type=radio], [role=radio], [class*='method'] label, [class*='option'] label")) {
      if (seen.has(el)) continue;
      const t = (el.tagName === "INPUT") ? el : null;
      const ok = visible(el) || (t && t.labels && t.labels.length && Array.from(t.labels).some(visible));
      if (!ok) continue;
      seen.add(el); n += 1;
    }
  };
  for (const sel of blocks) {
    let els = [];
    try { els = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    for (const el of els) if (visible(el)) countIn(el);
  }
  for (const sel of options) {
    let els = [];
    try { els = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    for (const el of els) {
      if (seen.has(el) || el.tagName === "IFRAME") continue;
      const ok = visible(el) || (el.labels && el.labels.length && Array.from(el.labels).some(visible));
      if (ok) { seen.add(el); n += 1; }
    }
  }
  return n;
}
"""
_BLOCK_HAS_CONTENT_JS = r"""
(selectors) => {
  for (const sel of selectors) {
    let els = [];
    try { els = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    for (const el of els) {
      const r = el.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) continue;
      const inner = el.querySelectorAll("input, iframe, label, img, [role=radio], [class*='method']").length;
      if (inner >= 1) return sel;
    }
  }
  return "";
}
"""
_LINKS_JS = r"""
(selectors) => {
  const out = [];
  const seen = new Set();
  for (const sel of selectors) {
    let els = [];
    try { els = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    for (const a of els) {
      const href = a.href || a.getAttribute("href") || "";
      if (!href || seen.has(href)) continue;
      const r = a.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) continue;
      seen.add(href);
      out.push(href);
      if (out.length >= 60) return out;
    }
  }
  return out;
}
"""


@dataclass
class WalkInput:
    url: str
    etld1: str
    identity: Identity
    flags: WalkFlags
    url_check: UrlCheck
    platform_id: str | None = None
    product_urls: list[str] = field(default_factory=list)
    credentials: Credentials | None = None
    # (homepage html, url) → platform id, used to pick the adapter when the profile has none
    platform_detect: Callable[[str, str], str | None] | None = None


@dataclass
class RegistrationEvent:
    email: str
    password: str
    status: StoreAccountStatus
    outcome: str


@dataclass
class WalkResult:
    adapter: str
    stop: Stop | None
    steps: list[StepTiming]
    journal: ActionJournal
    recorder: NetworkRecorder
    furthest: WalkStep
    final_url: str
    duration_ms: int
    capture: CheckoutCapture | None = None
    payment_fill: PaymentFillResult | None = None
    tokenizer: str = ""
    methods_revealed: bool = False
    used_account: bool = False
    registration: RegistrationEvent | None = None
    screenshot: bytes = b""
    dom: str = ""
    har: bytes | None = None
    dom_blocked: list[dict[str, Any]] = field(default_factory=list)
    blocked_posts: list[str] = field(default_factory=list)
    refused_hosts: list[str] = field(default_factory=list)
    cookie_names: list[str] = field(default_factory=list)
    product_url: str = ""
    checkout_url: str = ""
    checkout_entry_index: int = -1  # index into recorder.entries where the checkout began

    @property
    def reached_payment(self) -> bool:
        return self.stop is None

    @property
    def status(self) -> ScanStatus:
        return ScanStatus.REACHED_PAYMENT_STEP if self.stop is None else self.stop.status

    @property
    def coverage(self) -> Coverage:
        if self.stop is None:
            return Coverage.PAYMENT_STEP
        by_stop = self.stop.coverage
        by_walk = COVERAGE_AT_STEP[self.furthest]
        return max(by_stop, by_walk, key=_COVERAGE_ORDER.index)

    @property
    def blocked_by(self) -> str:
        if self.stop is None or self.stop.step != WalkStep.PROTECTION:
            return ""
        return self.stop.detail.split(":")[0][:40] if self.stop.detail else self.stop.reason


@dataclass
class WalkerConfig:
    walk_timeout_s: float = 90.0
    network_idle_timeout_ms: int = 15_000
    action_timeout_ms: int = 10_000
    max_clicks: int = 40
    max_steps: int = 6
    memory_limit_mb: float = 1024.0
    screenshot_max_bytes: int = 300 * 1024
    stop_detail_max_chars: int = 500
    product_candidates: int = 3
    current_year: int = 2026
    allow_private: bool = False
    rewrite: Callable[[str], str | None] | None = None
    global_candidates: list[str] = field(default_factory=list)


class CheckoutWalker:
    def __init__(
        self,
        pool: BrowserPool,
        *,
        dictionary: CheckoutDictionary,
        payment_values: TestPaymentValues,
        config: WalkerConfig,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pool = pool
        self.d = dictionary
        self.policy = ClickPolicy(dictionary)
        self.payment_values = payment_values
        self.cfg = config
        self.monotonic = monotonic

    # --- public ----------------------------------------------------------------------
    async def walk(self, inp: WalkInput) -> WalkResult:
        t0 = self.monotonic()
        journal = ActionJournal(lambda: self.monotonic() * 1000)
        journal.add("identity", **inp.identity.as_log())
        wc = await self.pool.new_walk_context(
            ContextOptions(
                locale=inp.identity.locale,
                accept_language=inp.identity.accept_language,
                allow_private=self.cfg.allow_private,
                rewrite=self.cfg.rewrite,
            )
        )
        page = await wc.new_page()
        recorder = NetworkRecorder(page)
        gp = GuardedPage(
            page=page,
            policy=self.policy,
            dictionary=self.d,
            identity=inp.identity,
            payment_values=self.payment_values,
            flags=inp.flags,
            journal=journal,
            url_check=inp.url_check,
            action_timeout_ms=self.cfg.action_timeout_ms,
            max_clicks=self.cfg.max_clicks,
            current_year=self.cfg.current_year,
        )
        run = _Run(self, gp, wc, recorder, inp, journal)
        stop: Stop | None = None
        try:
            await asyncio.wait_for(run.execute(), timeout=self.cfg.walk_timeout_s)
        except TimeoutError:
            stop = Stop(
                WalkStep.NAVIGATION,
                "timeout",
                f"walk deadline {self.cfg.walk_timeout_s:.0f}s exceeded during {run.step.value}",
                page_url=_safe_url(page),
            )
        except WalkStopped as exc:
            stop = exc.stop
        except PlaywrightError as exc:
            stop = stop_from_exception(exc, _safe_url(page))
        except Exception as exc:  # no silent failures (FR-CW-13): any bug is a stop record
            journal.add("exception", type=exc.__class__.__name__, text=str(exc)[:300])
            stop = stop_from_exception(exc, _safe_url(page))
        screenshot, dom = b"", ""
        if stop is not None:
            run.close_open_step()
            screenshot, dom = await self._evidence(page)
            journal.add("stop", step=stop.step.value, reason=stop.reason, detail=stop.detail[:200])
        dom_blocked: list[dict[str, Any]] = []
        try:
            dom_blocked = await gp.dom_blocked()
        except Exception:  # the page may be gone; the journal already holds the result
            dom_blocked = []
        cookie_names = await wc.cookie_names()
        final_url = _safe_url(page)
        har = await wc.close()
        return WalkResult(
            adapter=run.adapter.name,
            stop=stop,
            steps=run.timings,
            journal=journal,
            recorder=recorder,
            furthest=run.furthest,
            final_url=final_url,
            duration_ms=int((self.monotonic() - t0) * 1000),
            capture=run.capture,
            payment_fill=run.payment_fill,
            tokenizer=run.tokenizer,
            methods_revealed=run.methods_revealed,
            used_account=run.used_account,
            registration=run.registration,
            screenshot=screenshot,
            dom=dom,
            har=har,
            dom_blocked=dom_blocked,
            blocked_posts=list(wc.stats.blocked_posts),
            refused_hosts=list(wc.stats.refused_hosts),
            cookie_names=cookie_names,
            product_url=run.product_url,
            checkout_url=run.checkout_url,
            checkout_entry_index=run.checkout_entry_index,
        )

    async def _evidence(self, page: Page) -> tuple[bytes, str]:
        """Screenshot + DOM of the stop page; bounded so a hanging navigation cannot stall it."""
        try:
            shot = await asyncio.wait_for(
                screenshot_jpeg(page, self.cfg.screenshot_max_bytes), timeout=EVIDENCE_TIMEOUT_S
            )
        except Exception:
            shot = b""
        try:
            html = await asyncio.wait_for(page.content(), timeout=EVIDENCE_TIMEOUT_S)
        except Exception:
            html = ""
        clean, _ = sanitize_text(html[:400_000])
        return shot, clean


def _link_leaves_checkout(href: str, *, account_links: bool = False) -> bool:
    """A step link never points back at the homepage, the cart or (unless wanted) an account page."""
    path = (urlsplit(href).path or "/").rstrip("/")
    if path == "" or href.startswith("#"):
        return True
    if account_links:
        return bool(re.search(r"/(cart|basket|warenkorb|panier|logout)(/|$)", path, re.I))
    return bool(re.search(r"/(cart|basket|warenkorb|panier|account|login|logout)(/|$)", path, re.I))


def _safe_url(page: Page) -> str:
    try:
        return page.url
    except Exception:
        return ""


class _Run:
    """State of one walk; split from the walker so the walker stays reusable across walks."""

    def __init__(
        self,
        w: CheckoutWalker,
        gp: GuardedPage,
        wc: WalkContext,
        recorder: NetworkRecorder,
        inp: WalkInput,
        journal: ActionJournal,
    ) -> None:
        self.w = w
        self.gp = gp
        self.wc = wc
        self.recorder = recorder
        self.inp = inp
        self.journal = journal
        self.adapter: AdapterHints = adapter_for(inp.platform_id)
        self.step = WalkStep.NAVIGATION
        self.furthest = WalkStep.NAVIGATION
        self.timings: list[StepTiming] = []
        self.capture: CheckoutCapture | None = None
        self.payment_fill: PaymentFillResult | None = None
        self.tokenizer = ""
        self.methods_revealed = False
        self.used_account = False
        self.registration: RegistrationEvent | None = None
        self.product_url = ""
        self.checkout_url = ""
        self.checkout_entry_index = -1  # first network entry made from the checkout on
        self._base = inp.url
        self._tried: set[str] = set()
        self._open_step: tuple[WalkStep, float] | None = None

    # --- helpers --------------------------------------------------------------------
    def _begin(self, step: WalkStep) -> float:
        self.step = step
        if list(WalkStep).index(step) > list(WalkStep).index(self.furthest):
            self.furthest = step
        self.journal.add("step", step=step.value)
        t0 = self.w.monotonic()
        self._open_step = (step, t0)
        return t0

    def _end(self, step: WalkStep, t0: float) -> None:
        self._open_step = None
        self.timings.append(StepTiming(step.value, int((self.w.monotonic() - t0) * 1000)))

    def close_open_step(self) -> None:
        """A stop interrupts a step: its duration still goes into the timings (FR-CW-13)."""
        if self._open_step is not None:
            step, t0 = self._open_step
            self._end(step, t0)

    def _stop(self, step: WalkStep, reason: str, detail: str = "", **kw: Any) -> WalkStopped:
        return WalkStopped(Stop(step, reason, detail[:500], page_url=self.gp.url, **kw))

    async def _after_navigation(self, status: int | None) -> None:
        """Protection and HTTP checks after every navigation (FR-CW-06), then memory (FR-CW-10)."""
        await self.gp.wait_idle(self.w.cfg.network_idle_timeout_ms)
        title = await self.gp.title()
        html = await self.gp.content()
        text = await self.gp.body_text()
        block = detect_block(status=status, title=title, html=html, text=text, url=self.gp.url)
        if block is not None and block.reason == "captcha" and self.step == WalkStep.REGISTRATION:
            raise WalkStopped(
                Stop(
                    WalkStep.REGISTRATION,
                    "registration_captcha",
                    f"widget: {block.blocked_by}",
                    page_url=self.gp.url,
                )
            )
        if block is not None:
            raise WalkStopped(
                Stop(
                    WalkStep.PROTECTION,
                    block.reason,
                    f"{block.blocked_by}: {block.detail}"[:500],
                    page_url=self.gp.url,
                    http_status=status or 0,
                )
            )
        if status is not None and status >= 400:
            raise WalkStopped(
                Stop(
                    WalkStep.NAVIGATION,
                    "http_error",
                    f"HTTP {status} at {self.gp.url}",
                    page_url=self.gp.url,
                    http_status=status,
                )
            )
        heap = await BrowserPool.js_heap_mb(self.gp.page)
        if heap > self.w.cfg.memory_limit_mb:
            raise WalkStopped(
                Stop(
                    WalkStep.NAVIGATION,
                    "memory_limit",
                    f"JS heap {heap:.0f} MB > {self.w.cfg.memory_limit_mb:.0f} MB",
                    page_url=self.gp.url,
                )
            )

    async def _goto(self, url: str, step: WalkStep) -> None:
        resp = await self.gp.goto(url, step=step)
        if resp is not None and resp.request.is_navigation_request():
            await self.recorder.finish_main(resp)
        await self._after_navigation(resp.status if resp is not None else None)

    async def _first_visible(self, selectors: tuple[str, ...]) -> GuardedLocator | None:
        for sel in selectors:
            loc = self.gp.locator(sel)
            try:
                n = await loc.count()
            except PlaywrightError:
                continue
            for i in range(min(n, 8)):
                cand = loc.nth(i)
                try:
                    if await cand.is_visible():
                        return cand
                except PlaywrightError:
                    continue
        return None

    async def _visible_count(self, selectors: tuple[str, ...]) -> int:
        try:
            return int(await self.gp.page.evaluate(_VISIBLE_COUNT_JS, list(selectors)))
        except PlaywrightError:
            return 0

    async def _links(self, selectors: tuple[str, ...]) -> list[str]:
        try:
            raw = await self.gp.page.evaluate(_LINKS_JS, list(selectors))
        except PlaywrightError:
            return []
        own = self.inp.etld1
        out: list[str] = []
        for href in raw:
            h = (urlsplit(str(href)).hostname or "").lower()
            if h and (h == own or h.endswith("." + own)):
                out.append(str(href))
        return out

    async def _click_first_allowed(
        self,
        candidates: list[tuple[GuardedLocator, Any]],
        purpose: Purpose,
    ) -> bool:
        """Click the first candidate the policy allows; an all-refused list raises the stop."""
        refused = None
        for target, control in candidates:
            decision = self.w.policy.decide(control, purpose)
            if decision.allowed:
                await self.gp.click(target, purpose)
                return True
            refused = refused or (target, decision)
        if refused is not None:
            target, _ = refused
            await self.gp.click(target, purpose)  # raises GuardrailStop with the journal entry
        return False

    def _ranked(
        self, controls: list[tuple[GuardedLocator, Any]], *, account_links: bool = False
    ) -> list[tuple[GuardedLocator, Any]]:
        """Submit buttons in forms first, then buttons, then links; never a link to the homepage."""
        ranked: list[tuple[int, int, GuardedLocator, Any]] = []
        for i, (loc, control) in enumerate(controls):
            if control.tag == "a" and control.href:
                if _link_leaves_checkout(control.href, account_links=account_links):
                    continue
            rank = 2
            if control.tag in {"button", "input"} and control.type in {"submit", ""}:
                rank = 0 if (control.form_action or control.form_id or control.form_classes) else 1
            elif control.tag == "button":
                rank = 1
            ranked.append((rank, i, loc, control))
        ranked.sort(key=lambda r: (r[0], r[1]))
        return [(loc, control) for _, _, loc, control in ranked]

    async def _click_selectors_or_phrases(
        self, selectors: tuple[str, ...], section: str, purpose: Purpose
    ) -> bool:
        target = await self._first_visible(selectors)
        if target is not None:
            await self.gp.click(target, purpose)
            return True
        controls = self._ranked(await self.gp.find_controls(section))
        if controls:
            return await self._click_first_allowed(controls, purpose)
        return False

    async def _submit_password_form(self, primary: Purpose, failure: Stop) -> None:
        """Press the submit control of the form holding the password field (login / register)."""
        submit = await self._first_visible(
            (
                "form:has(input[type=password]) button[type=submit]",
                "form:has(input[type=password]) input[type=submit]",
                "form:has(input[type=password]) button:not([type=button])",
            )
        )
        if submit is None:
            raise WalkStopped(failure)
        control = await submit.describe()
        for purpose in (primary, Purpose.STEP):
            if self.w.policy.decide(control, purpose).allowed:
                await self.gp.click(submit, purpose)
                return
        await self.gp.click(submit, primary)  # refused: GuardrailStop carries the reason

    def _text_has(self, text: str, section: str) -> str | None:
        return self.w.d.match(text[:30_000], section)

    # --- the sequence -----------------------------------------------------------------
    async def execute(self) -> None:
        t = self._begin(WalkStep.NAVIGATION)
        await self._goto(self.inp.url, WalkStep.NAVIGATION)
        self._base = self.gp.url
        if self.inp.platform_id is None and self.inp.platform_detect is not None:
            detected = self.inp.platform_detect(await self.gp.content(), self.gp.url)
            if detected:
                self.adapter = adapter_for(detected)
                self.journal.add("platform_detected", platform=detected, adapter=self.adapter.name)
        self._end(WalkStep.NAVIGATION, t)

        t = self._begin(WalkStep.PRODUCT)
        await self._product()
        self._end(WalkStep.PRODUCT, t)

        t = self._begin(WalkStep.CART)
        await self._cart()
        self._end(WalkStep.CART, t)

        t = self._begin(WalkStep.CHECKOUT)
        self.checkout_entry_index = len(self.recorder.entries)
        await self._checkout()
        self._end(WalkStep.CHECKOUT, t)

        await self._address_shipping_and_steps()

        t = self._begin(WalkStep.PAYMENT)
        await self._payment()
        self._end(WalkStep.PAYMENT, t)

    # --- product ----------------------------------------------------------------------
    async def _product_candidates(self) -> list[str]:
        own = self.inp.etld1
        cands: list[str] = []
        for u in self.inp.product_urls:
            h = (urlsplit(u).hostname or "").lower()
            if h == own or h.endswith("." + own):
                cands.append(u)
        cands.extend(await self._links(self.adapter.product_links))
        all_links = await self._links(("a[href]",))
        for u in all_links:
            path = urlsplit(u).path
            if any(re.search(p, path) for p in self.adapter.product_url_patterns):
                cands.append(u)
        out: list[str] = []
        for u in cands:
            if _NON_PRODUCT_PATH_RE.search(urlsplit(u).path or "/"):
                continue
            if u.rstrip("/") == self._base.rstrip("/"):
                continue
            if u not in out:
                out.append(u)
        return out

    async def _product(self) -> None:
        candidates = await self._product_candidates()
        if not candidates:
            raise self._stop(
                WalkStep.PRODUCT, "no_product_found", "no product link on the homepage"
            )
        last_reason = ("no_product_found", "no add-to-cart control on candidate pages")
        for url in candidates[: self.w.cfg.product_candidates]:
            await self._goto(url, WalkStep.PRODUCT)
            text = await self.gp.body_text()
            await self._select_variants()
            add = await self._first_visible(self.adapter.add_to_cart)
            controls = [] if add is not None else await self.gp.find_controls("add_to_cart")
            if add is None and not controls:
                if m := self._text_has(text, "out_of_stock"):
                    last_reason = ("out_of_stock", f"'{m}' on {url}")
                elif m := self._text_has(text, "price_on_request"):
                    last_reason = ("price_on_request", f"'{m}' on {url}")
                continue
            self.product_url = url
            if add is not None:
                await self.gp.click(add, Purpose.ADD_TO_CART)
            else:
                await self._click_first_allowed(controls, Purpose.ADD_TO_CART)
            await self.gp.wait_idle(self.w.cfg.network_idle_timeout_ms)
            return
        raise self._stop(WalkStep.PRODUCT, last_reason[0], last_reason[1])

    async def _select_variants(self) -> None:
        for sel in self.adapter.variant_selects:
            loc = self.gp.locator(sel)
            try:
                n = await loc.count()
            except PlaywrightError:
                continue
            for i in range(min(n, 4)):
                cand = loc.nth(i)
                if await cand.is_visible():
                    await self.gp.select_first_option(cand)
        for sel in self.adapter.variant_options:
            target = await self._first_visible((sel,))
            if target is not None:
                try:
                    await self.gp.click(target, Purpose.VARIANT)
                except WalkStopped:
                    raise
                except PlaywrightError:
                    continue
                break

    # --- cart -------------------------------------------------------------------------
    async def _cart(self) -> None:
        if self.adapter.cart_path:
            await self._goto(urljoin(self._base, self.adapter.cart_path), WalkStep.CART)
        else:
            links = await self._links(self.adapter.cart_links)
            if links:
                await self._goto(links[0], WalkStep.CART)
            elif not await self._click_selectors_or_phrases((), "cart_words", Purpose.CART):
                raise self._stop(WalkStep.CART, "add_to_cart_failed", "no cart link after add")
            else:
                await self._after_navigation(None)
        text = await self.gp.body_text()
        if await self._visible_count(self.adapter.cart_items) == 0:
            if m := self._text_has(text, "min_order_value"):
                raise self._stop(WalkStep.CART, "min_order_value", f"'{m}'")
            raise self._stop(WalkStep.CART, "cart_empty_after_add", "no cart line items")
        if m := self._text_has(text, "min_order_value"):
            links = await self._links(self.adapter.checkout_links)
            controls = await self.gp.find_controls("checkout_words")
            if not links and not controls:
                raise self._stop(WalkStep.CART, "min_order_value", f"'{m}' and no checkout control")
            self.journal.add("note", text=f"min order text present: {m}")

    # --- checkout and account wall ----------------------------------------------------
    async def _checkout(self) -> None:
        if self.adapter.checkout_path:
            await self._goto(urljoin(self._base, self.adapter.checkout_path), WalkStep.CHECKOUT)
        else:
            links = await self._links(self.adapter.checkout_links)
            if links:
                await self._goto(links[0], WalkStep.CHECKOUT)
            elif await self._click_selectors_or_phrases(
                self.adapter.checkout_links, "checkout_words", Purpose.CHECKOUT
            ):
                await self._after_navigation(None)
            else:
                raise self._stop(WalkStep.CHECKOUT, "checkout_not_found", "no checkout link")
        self.checkout_url = self.gp.url
        await self._resolve_account_wall()

    async def _address_fields_visible(self) -> bool:
        wanted = (BuyerField.STREET_LINE, BuyerField.POSTCODE, BuyerField.FIRST_NAME)
        hits = 0
        for kind in wanted:
            if await self._first_visible(self.adapter.fields.get(kind, ())) is not None:
                hits += 1
        return hits >= 2

    async def _login_form_visible(self) -> bool:
        if await self._first_visible(self.adapter.login_form) is not None:
            return True
        pw = await self._first_visible(("input[type=password]",))
        return pw is not None

    async def _resolve_account_wall(self) -> None:
        if await self._payment_visible() or await self._address_fields_visible():
            if await self._first_visible(self.adapter.guest_controls) is not None:
                await self._choose_guest()
            return
        if await self._first_visible(self.adapter.guest_controls) is not None:
            await self._choose_guest()
            return
        if await self._click_guest_phrase():
            return
        text = await self.gp.body_text()
        if await self._login_form_visible() or self._text_has(text, "login_wall"):
            await self._account_flow()
            return
        if not await self._address_fields_visible() and not await self._payment_visible():
            raise self._stop(
                WalkStep.CHECKOUT, "checkout_not_found", "neither address form nor login wall"
            )

    async def _choose_guest(self) -> None:
        target = await self._first_visible(self.adapter.guest_controls)
        if target is None:
            return
        control = await target.describe()
        if control.type in {"checkbox", "radio"}:
            decision = self.w.policy.decide(control, Purpose.GUEST)
            if not decision.allowed:
                await self.gp.click(target, Purpose.GUEST)  # raises GuardrailStop
            if not await target.is_checked():
                try:
                    await target._locator.first.check(timeout=self.w.cfg.action_timeout_ms)
                    self.journal.add("guest", control=control.describe())
                except PlaywrightError as exc:
                    self.journal.add("guest_check_failed", error=str(exc)[:120])
            return
        await self.gp.click(target, Purpose.GUEST)
        await self.gp.wait_idle(self.w.cfg.network_idle_timeout_ms)

    async def _click_guest_phrase(self) -> bool:
        controls = self._ranked(await self.gp.find_controls("guest_checkout"), account_links=True)
        if not controls:
            return False
        await self._click_first_allowed(controls, Purpose.GUEST)
        await self.gp.wait_idle(self.w.cfg.network_idle_timeout_ms)
        return True

    async def _account_flow(self) -> None:
        """FR-CW-12: existing system account → login; none → registration when allowed."""
        cred = self.inp.credentials
        if cred is not None and cred.status != StoreAccountStatus.FAILED:
            await self._login(cred)
            return
        if not self.inp.flags.allow_account_registration:
            raise self._stop(
                WalkStep.CHECKOUT,
                "guest_unavailable_registration_disabled",
                "login wall, no guest option, allow_account_registration=false",
            )
        await self._register()

    async def _login(self, cred: Credentials) -> None:
        email = await self._first_visible(self.adapter.login_fields.get("email", ()))
        password = await self._first_visible(self.adapter.login_fields.get("password", ()))
        if email is None or password is None:
            raise self._stop(WalkStep.CHECKOUT, "login_failed", "login form fields not found")
        if cred.email != self.gp.identity.email:
            raise self._stop(
                WalkStep.CHECKOUT, "login_failed", "stored account e-mail is not the probe address"
            )
        await self.gp.fill_credential(email, "email", cred.email)
        await self.gp.fill_credential(password, "password", cred.password)
        await self._submit_password_form(
            Purpose.LOGIN,
            Stop(WalkStep.CHECKOUT, "login_failed", "no login button", page_url=self.gp.url),
        )
        await self.gp.wait_idle(self.w.cfg.network_idle_timeout_ms)
        await self._after_navigation(None)
        if await self._login_form_visible() and not await self._address_fields_visible():
            raise self._stop(WalkStep.CHECKOUT, "login_failed", "login form still shown")
        self.used_account = True
        self.journal.add("login", email=cred.email)
        if not await self._address_fields_visible() and not await self._payment_visible():
            await self._goto(self.checkout_url or self.gp.url, WalkStep.CHECKOUT)

    async def _register(self) -> None:
        t = self._begin(WalkStep.REGISTRATION)
        try:
            if await self._first_visible(self.adapter.register_form) is None:
                controls = self._ranked(await self.gp.find_controls("register"), account_links=True)
                target = await self._first_visible(self.adapter.register_controls)
                if target is not None:
                    await self.gp.click(target, Purpose.REGISTER)
                elif controls:
                    await self._click_first_allowed(controls, Purpose.REGISTER)
                else:
                    raise self._stop(
                        WalkStep.REGISTRATION, "registration_form_unknown", "no register link"
                    )
                await self.gp.wait_idle(self.w.cfg.network_idle_timeout_ms)
                await self._after_navigation(None)
            html = await self.gp.content()
            text = await self.gp.body_text()
            if cap := has_captcha_widget(html):
                raise self._stop(WalkStep.REGISTRATION, "registration_captcha", f"widget: {cap}")
            if m := self._text_has(text, "sms_verification"):
                raise self._stop(WalkStep.REGISTRATION, "registration_sms_required", f"'{m}'")
            if m := self._text_has(text, "documents_required"):
                raise self._stop(WalkStep.REGISTRATION, "registration_documents_required", f"'{m}'")
            if await detect_tokenizer(self.gp):
                raise self._stop(
                    WalkStep.REGISTRATION,
                    "registration_payment_required",
                    "card fields inside the registration form",
                )
            pw_field = await self._first_visible(self.adapter.register_fields.get("password", ()))
            if pw_field is None:
                raise self._stop(
                    WalkStep.REGISTRATION, "registration_form_unknown", "no password field"
                )
            password = self.inp.credentials.password if self.inp.credentials else _new_password()
            await self._fill_address()
            email = await self._first_visible(self.adapter.fields.get(BuyerField.EMAIL, ()))
            if email is not None:
                await self.gp.fill_credential(email, "email", self.gp.identity.email)
            await self.gp.fill_credential(pw_field, "password", password)
            confirm = await self._first_visible(self.adapter.register_fields.get("confirm", ()))
            if confirm is not None:
                await self.gp.fill_credential(confirm, "password", password)
            await self._tick_required()
            await self._submit_password_form(
                Purpose.REGISTER,
                Stop(
                    WalkStep.REGISTRATION,
                    "registration_form_unknown",
                    "no submit button",
                    page_url=self.gp.url,
                ),
            )
            await self.gp.wait_idle(self.w.cfg.network_idle_timeout_ms)
            await self._after_navigation(None)
            html = await self.gp.content()
            text = await self.gp.body_text()
            if cap := has_captcha_widget(html):
                self.registration = RegistrationEvent(
                    self.gp.identity.email, password, StoreAccountStatus.FAILED, "captcha"
                )
                raise self._stop(WalkStep.REGISTRATION, "registration_captcha", f"widget: {cap}")
            if m := self._text_has(text, "email_verification"):
                self.registration = RegistrationEvent(
                    self.gp.identity.email,
                    password,
                    StoreAccountStatus.PENDING_VERIFICATION,
                    "email_verification",
                )
                raise self._stop(WalkStep.REGISTRATION, "email_verification_pending", f"'{m}'")
            if m := self._text_has(text, "sms_verification"):
                self.registration = RegistrationEvent(
                    self.gp.identity.email, password, StoreAccountStatus.FAILED, "sms"
                )
                raise self._stop(WalkStep.REGISTRATION, "registration_sms_required", f"'{m}'")
            if await self._first_visible(self.adapter.register_form) is not None and (
                await self._first_visible(("input[type=password]",)) is not None
            ):
                self.registration = RegistrationEvent(
                    self.gp.identity.email, password, StoreAccountStatus.FAILED, "form_rejected"
                )
                raise self._stop(
                    WalkStep.REGISTRATION,
                    "registration_form_unknown",
                    "registration form still shown after submit",
                )
            self.registration = RegistrationEvent(
                self.gp.identity.email, password, StoreAccountStatus.ACTIVE, "registered"
            )
            self.used_account = True
            self.journal.add("registered", email=self.gp.identity.email)
            if not await self._address_fields_visible() and not await self._payment_visible():
                await self._goto(self.checkout_url or self.gp.url, WalkStep.CHECKOUT)
                await self._resolve_after_login()
        finally:
            self._end(WalkStep.REGISTRATION, t)

    async def _resolve_after_login(self) -> None:
        if await self._first_visible(self.adapter.guest_controls) is not None:
            await self._choose_guest()

    # --- address, shipping, steps -----------------------------------------------------
    async def _fill_address(self) -> int:
        if not self.inp.flags.allow_shipping_step_fill:
            self.journal.add(
                "fill_refused", field="address", reason="allow_shipping_step_fill=false"
            )
            return 0
        filled = 0
        for sel in self.adapter.salutation_selects:
            target = await self._first_visible((sel,))
            if target is not None:
                await self.gp.select_first_option(target)
                break
        for sel in self.adapter.country_selects:
            target = await self._first_visible((sel,))
            if target is not None:
                await self.gp.select_country(target)
                await self.gp.wait_idle(3_000)
                break
        order = [
            BuyerField.EMAIL,
            BuyerField.EMAIL_CONFIRM,
            BuyerField.FIRST_NAME,
            BuyerField.LAST_NAME,
            BuyerField.STREET_LINE,
            BuyerField.HOUSE_NUMBER,
            BuyerField.POSTCODE,
            BuyerField.CITY,
            BuyerField.PHONE,
        ]
        done: set[BuyerField] = set()
        for kind in order:
            target = await self._first_empty_visible(self.adapter.fields.get(kind, ()))
            if target is not None and await self.gp.fill_buyer_field(target, kind):
                filled += 1
                done.add(kind)
        if BuyerField.FIRST_NAME not in done and BuyerField.LAST_NAME not in done:
            target = await self._first_empty_visible(
                self.adapter.fields.get(BuyerField.FULL_NAME, ())
            )
            if target is not None and await self.gp.fill_buyer_field(target, BuyerField.FULL_NAME):
                filled += 1
        for sel in self.adapter.region_selects:
            target = await self._first_visible((sel,))
            if target is not None:
                await self.gp.select_first_option(target)
                break
        return filled

    async def _first_empty_visible(self, selectors: tuple[str, ...]) -> GuardedLocator | None:
        for sel in selectors:
            loc = self.gp.locator(sel)
            try:
                n = await loc.count()
            except PlaywrightError:
                continue
            for i in range(min(n, 6)):
                cand = loc.nth(i)
                try:
                    if not await cand.is_visible():
                        continue
                    if (await cand._locator.input_value(timeout=1_000)).strip():
                        continue
                except PlaywrightError:
                    continue
                return cand
        return None

    async def _tick_required(self) -> int:
        n = 0
        for sel in self.adapter.required_checkboxes:
            loc = self.gp.locator(sel)
            try:
                count = await loc.count()
            except PlaywrightError:
                continue
            for i in range(min(count, 6)):
                cand = loc.nth(i)
                if await cand.is_visible() and await self.gp.check_required_checkbox(cand):
                    n += 1
        return n

    async def _select_shipping(self) -> bool:
        for sel in self.adapter.shipping_options:
            loc = self.gp.locator(sel)
            try:
                count = await loc.count()
            except PlaywrightError:
                continue
            for i in range(min(count, 6)):
                cand = loc.nth(i)
                try:
                    if await cand.is_checked():
                        return True
                except PlaywrightError:
                    continue
            for i in range(min(count, 6)):
                cand = loc.nth(i)
                try:
                    await self.gp.select_shipping(cand)
                    await self.gp.wait_idle(3_000)
                    return True
                except WalkStopped:
                    raise
                except PlaywrightError:
                    continue
        return False

    async def _payment_visible(self) -> bool:
        if await self._visible_count(self.adapter.payment_options) > 0:
            return True
        try:
            sel = await self.gp.page.evaluate(
                _BLOCK_HAS_CONTENT_JS, list(self.adapter.payment_blocks)
            )
        except PlaywrightError:
            return False
        return bool(sel)

    async def _fingerprint(self) -> str:
        """URL plus a hash of the visible text: the same button on a new step is a new control."""
        text = await self.gp.body_text(limit=5_000)
        return (
            f"{self.gp.url}|{hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:12]}"
        )

    async def _method_count(self) -> int:
        """Visible payment-method choices (radios / labelled options), iframes excluded."""
        try:
            return int(
                await self.gp.page.evaluate(
                    _METHOD_COUNT_JS,
                    [list(self.adapter.payment_blocks), list(self.adapter.payment_options)],
                )
            )
        except PlaywrightError:
            return 0

    async def _unfilled_required(self) -> list[str]:
        try:
            return [str(x) for x in await self.gp.page.evaluate(_VISIBLE_REQUIRED_JS)]
        except PlaywrightError:
            return []

    async def _advance(self) -> bool:
        """Press one step control: adapter buttons, then dictionary matches ordered by trust.

        Submit buttons inside the form that holds the filled fields come first,
        then other buttons, then links; a control already pressed on this URL
        without effect is not pressed again (decoys with a cancelled submit).
        """
        key = await self._fingerprint()
        target = await self._first_visible(self.adapter.step_buttons)
        if target is not None:
            control = await target.describe()
            if f"{key}|{control.describe()}" not in self._tried:
                self._tried.add(f"{key}|{control.describe()}")
                await self.gp.click(target, Purpose.STEP)
                return True
        controls = self._ranked(await self.gp.find_controls("step_actions"))
        ordered = [
            (loc, control)
            for loc, control in controls
            if f"{key}|{control.describe()}" not in self._tried
        ]
        if not ordered:
            return False
        self._tried.add(f"{key}|{ordered[0][1].describe()}")
        for _, control in ordered:  # the first allowed one is pressed; mark it tried
            decision = self.w.policy.decide(control, Purpose.STEP)
            if decision.allowed:
                self._tried.add(f"{key}|{control.describe()}")
                break
        return await self._click_first_allowed(ordered, Purpose.STEP)

    async def _address_shipping_and_steps(self) -> None:
        """Fill what is visible, pick shipping, press step controls until the payment block shows.

        One-page checkouts show the payment block from the start; the address
        is still filled first (FR-CW-05) because the offered methods may depend
        on the country, and the shipping option is picked before capturing.
        """
        for _ in range(self.w.cfg.max_steps + 1):
            if await self._address_fields_visible():
                t = self._begin(WalkStep.ADDRESS)
                await self._fill_address()
                await self._tick_required()
                self._end(WalkStep.ADDRESS, t)
            text = await self.gp.body_text()
            shipping_words = self._text_has(text, "shipping_words")
            picked = False
            if shipping_words or await self._visible_count(self.adapter.shipping_options):
                t = self._begin(WalkStep.SHIPPING)
                picked = await self._select_shipping()
                self._end(WalkStep.SHIPPING, t)
            if await self._payment_visible():
                return
            if (
                not picked
                and shipping_words
                and await self._visible_count(("input[type=radio]",)) == 0
                and not await self._address_fields_visible()
            ):
                raise self._stop(
                    WalkStep.SHIPPING, "no_shipping_option", "shipping section without options"
                )
            if not self.inp.flags.allow_shipping_step_fill:
                raise self._stop(
                    WalkStep.ADDRESS, "other", "allow_shipping_step_fill=false: cannot proceed"
                )
            url_before = self.gp.url
            if not await self._advance():
                missing = await self._unfilled_required()
                if missing:
                    raise self._stop(
                        WalkStep.ADDRESS, "required_field_unmapped", ", ".join(missing)
                    )
                raise self._stop(
                    WalkStep.PAYMENT,
                    "payment_step_not_detected",
                    "no step control, no payment block",
                )
            await self.gp.wait_idle(self.w.cfg.network_idle_timeout_ms)
            await self._after_navigation(None)
            if self.gp.url == url_before and not await self._payment_visible():
                missing = await self._unfilled_required()
                if missing:
                    raise self._stop(
                        WalkStep.ADDRESS, "required_field_unmapped", ", ".join(missing)
                    )
        if not await self._payment_visible():
            raise self._stop(
                WalkStep.PAYMENT,
                "payment_step_not_detected",
                f"{self.w.cfg.max_steps} step presses without a payment block",
            )

    # --- payment step -----------------------------------------------------------------
    async def _payment(self) -> None:
        await self.gp.enter_capture_phase()
        self.methods_revealed = await self._method_count() > 0
        self.tokenizer = await detect_tokenizer(self.gp)
        self.capture = await self._capture()
        if not self.methods_revealed and self.tokenizer:
            self.payment_fill = await fill_test_card(self.gp)
            if self.payment_fill.number_filled:
                await self.gp.wait_idle(self.w.cfg.network_idle_timeout_ms)
                self.capture = await self._capture()
        self.journal.add(
            "payment_step",
            url=self.gp.url,
            revealed=self.methods_revealed,
            tokenizer=self.tokenizer,
            labels=len(self.capture.labels) if self.capture else 0,
        )

    async def _capture(self) -> CheckoutCapture:
        return await capture_page(
            self.gp.page,
            page_type="payment_step",
            global_candidates=self.w.cfg.global_candidates,
            cookie_names=await self.wc.cookie_names(),
            headers=self.recorder.main_headers,
            screenshot_max_bytes=self.w.cfg.screenshot_max_bytes,
            favicon_fetch=api_favicon_fetcher(
                self.gp.page.context,
                allow_private=self.w.cfg.allow_private,
                rewrite=self.w.cfg.rewrite,
            ),
        )


def _new_password() -> str:
    from payintel.crawl.checkout.accounts import AccountManager

    return AccountManager.new_password()
