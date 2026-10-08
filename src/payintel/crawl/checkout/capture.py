"""Observations of a checkout page (FR-CW-07) and their evidence (FR-CW-08).

`NetworkRecorder` listens to every request/response of the page from the
first navigation on: URL, method, resource type, initiator frame, status,
host, plus the TLS details and headers of the main document and the redirect
chain. `capture_page` then reads the DOM once the payment step is reached:
scripts, iframes, form actions, known JS globals, visible payment-method
labels and logos (alt, title, file names, text), favicon (URL + SHA-256),
cookie names, a JPEG screenshot (≤300 KB) and the sanitised DOM of the
payment block. Everything is read-only; nothing here clicks or submits.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, Request, Response
from playwright.async_api import Error as PlaywrightError

from payintel.crawl.checkout.guardrails.injection import Rewriter, is_private_host
from payintel.crawl.light.html import HtmlFeatures, extract
from payintel.crawl.sanitize import SanitizeStats, sanitize_headers, sanitize_text
from payintel.detect.engine import PageSignals

MAX_ENTRIES = 2_000
MAX_DOM_BYTES = 300 * 1024
MAX_FAVICON_BYTES = 64 * 1024
MAX_LABELS = 300
PAYMENT_BLOCK_SELECTORS = (
    "#payment",
    ".woocommerce-checkout-payment",
    "#checkout-payment-method-load",
    ".checkout-payment-method",
    ".payment-methods",
    ".payment-options",
    "#payment-method",
    "[data-step=payment]",
    "form#payment-form",
    ".confirm-payment",
    "[id*=payment-method]",
    "[class*=payment-method]",
    "[id*=payment]",
    "[class*=payment]",
    "[id*=zahlung]",
    "[class*=zahlung]",
    "[id*=paiement]",
    "[class*=paiement]",
    "[id*=betaling]",
    "[class*=betaling]",
    "[id*=pago]",
    "[class*=pago]",
    "[id*=pagamento]",
    "[class*=pagamento]",
)

_LABELS_JS = r"""
(selectors) => {
  const out = new Set();
  const add = (s) => { if (s) { s = String(s).trim().replace(/\s+/g, " "); if (s.length > 1 && s.length < 120) out.add(s); } };
  const base = (u) => { try { return decodeURIComponent((u.split("?")[0].split("#")[0].split("/").pop() || "")); } catch (e) { return ""; } };
  let roots = [];
  for (const sel of selectors) { try { roots = roots.concat(Array.from(document.querySelectorAll(sel))); } catch (e) {} }
  if (!roots.length) roots = [document.body];
  for (const root of roots) {
    if (!root) continue;
    for (const img of root.querySelectorAll("img, svg, picture source")) {
      add(img.getAttribute("alt")); add(img.getAttribute("title")); add(img.getAttribute("aria-label"));
      const src = img.getAttribute("src") || img.getAttribute("data-src") || img.getAttribute("srcset") || "";
      if (src) add(base(src.split(",")[0].trim().split(" ")[0]));
      const svgTitle = img.querySelector ? img.querySelector("title") : null; if (svgTitle) add(svgTitle.textContent);
    }
    for (const el of root.querySelectorAll("label, legend, h1, h2, h3, h4, li, span, p, div, a, button, option")) {
      if (el.children.length > 3) continue;
      const t = (el.innerText || "").trim(); if (t && t.length < 120) add(t);
      add(el.getAttribute("aria-label")); add(el.getAttribute("title")); add(el.getAttribute("data-method")); add(el.getAttribute("data-payment-method"));
    }
    for (const input of root.querySelectorAll("input[type=radio], input[type=checkbox]")) {
      add(input.value); add(input.getAttribute("data-method")); add(input.id);
    }
  }
  return Array.from(out);
}
"""
_PAYMENT_BLOCK_JS = r"""
(selectors) => {
  let best = null;
  for (const sel of selectors) {
    let nodes = [];
    try { nodes = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    for (const n of nodes) {
      const html = n.outerHTML || "";
      if (html.length < 200) continue;
      const inputs = n.querySelectorAll("input, iframe, img, label").length;
      if (!best || inputs > best.inputs) best = { html, inputs, selector: sel };
    }
    if (best && best.inputs >= 2) break;
  }
  return best;
}
"""
_FAVICON_JS = r"""
() => {
  const link = document.querySelector(
    "link[rel~='icon'], link[rel='shortcut icon'], link[rel='apple-touch-icon']");
  return link ? link.href : new URL("/favicon.ico", location.href).href;
}
"""
_GLOBALS_JS = r"""
(names) => {
  const found = [];
  for (const name of names) {
    try {
      let obj = window; let ok = true;
      for (const part of name.split(".")) { if (obj === null || obj === undefined || !(part in Object(obj))) { ok = false; break; } obj = obj[part]; }
      if (ok && obj !== undefined) found.push(name);
    } catch (e) {}
  }
  return found;
}
"""


@dataclass
class NetworkEntry:
    url: str
    method: str
    resource_type: str
    initiator: str
    host: str
    status: int | None = None
    is_navigation: bool = False
    frame_url: str = ""


@dataclass
class TlsInfo:
    protocol: str = ""
    issuer: str = ""
    subject: str = ""
    valid_from: float = 0.0
    valid_to: float = 0.0


class NetworkRecorder:
    """Attach to a page before the first navigation; collects everything until detached."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.entries: list[NetworkEntry] = []
        self.redirects: list[tuple[str, int]] = []
        self.main_headers: dict[str, str] = {}
        self.main_status: int | None = None
        self.main_url: str = ""
        self.tls: TlsInfo | None = None
        self._by_request: dict[int, NetworkEntry] = {}
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    def _on_request(self, request: Request) -> None:
        if len(self.entries) >= MAX_ENTRIES:
            return
        frame_url = ""
        with contextlib.suppress(PlaywrightError):  # detached frame: no initiator
            frame_url = request.frame.url
        entry = NetworkEntry(
            url=request.url[:2048],
            method=request.method,
            resource_type=request.resource_type,
            initiator=urlsplit(frame_url).hostname or "",
            host=(urlsplit(request.url).hostname or "").lower(),
            is_navigation=request.is_navigation_request(),
            frame_url=frame_url[:512],
        )
        self.entries.append(entry)
        self._by_request[id(request)] = entry

    def _on_response(self, response: Response) -> None:
        entry = self._by_request.get(id(response.request))
        if entry is not None:
            entry.status = response.status
        req = response.request
        if req.is_navigation_request() and req.frame == self.page.main_frame:
            if 300 <= response.status < 400:
                self.redirects.append((response.url[:1024], response.status))
            else:
                self.main_status = response.status
                self.main_url = response.url
                self.main_headers = {k.lower(): v for k, v in response.headers.items()}

    async def finish_main(self, response: Response | None) -> None:
        """Record TLS details of the document response (needs an await, so not in the handler)."""
        if response is None:
            return
        try:
            details = await response.security_details()
        except PlaywrightError:
            details = None
        if details:
            self.tls = TlsInfo(
                protocol=str(details.get("protocol", "")),
                issuer=str(details.get("issuer", "")),
                subject=str(details.get("subjectName", "")),
                valid_from=float(details.get("validFrom", 0) or 0),
                valid_to=float(details.get("validTo", 0) or 0),
            )

    def hosts(self) -> set[str]:
        return {e.host for e in self.entries if e.host}

    def third_party(self, own_etld1: str) -> dict[str, list[NetworkEntry]]:
        out: dict[str, list[NetworkEntry]] = {}
        for e in self.entries:
            if not e.host or e.host == own_etld1 or e.host.endswith("." + own_etld1):
                continue
            out.setdefault(e.host, []).append(e)
        return out


@dataclass
class CheckoutCapture:
    url: str
    page_type: str  # checkout | payment_step
    title: str = ""
    raw_html: str = ""
    clean_html: str = ""
    sanitize: SanitizeStats = field(default_factory=SanitizeStats)
    features: HtmlFeatures | None = None
    js_globals: set[str] = field(default_factory=set)
    labels: list[str] = field(default_factory=list)
    payment_block_html: str = ""
    payment_block_selector: str = ""
    screenshot_jpeg: bytes = b""
    favicon_url: str = ""
    favicon_sha256: str = ""
    cookie_names: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    body_text: str = ""
    currency_text_sample: str = ""

    def signals(self, network_hosts: set[str]) -> PageSignals:
        f = self.features
        return PageSignals(
            page_type=self.page_type,
            url=self.url,
            html=self.raw_html,
            headers=self.headers,
            cookie_names=set(self.cookie_names),
            script_srcs=f.scripts_src if f else [],
            iframe_srcs=f.iframes_src if f else [],
            form_actions=f.forms_action if f else [],
            network_hosts=network_hosts | (f.external_hosts() if f else set()),
            js_globals=self.js_globals | (f.js_globals() if f else set()),
            favicon_sha256=self.favicon_sha256 or None,
            checkout_labels=self.labels,
        )


FaviconFetcher = Callable[[str], Awaitable[bytes | None]]


def api_favicon_fetcher(
    context: BrowserContext,
    *,
    allow_private: bool = False,
    rewrite: Rewriter | None = None,
    max_bytes: int = MAX_FAVICON_BYTES,
) -> FaviconFetcher:
    """Fetch the favicon with the context's request API (same cookies, no page script).

    Chromium fetches `/favicon.ico` itself for the tab; a second in-page fetch
    of the same URL is coalesced with that browser-level request and fails
    under route interception, so the bytes are read from Python instead. The
    egress rule (NFR-S-09) and the test rewrite are applied the same way as in
    the route handler.
    """

    async def fetch(url: str) -> bytes | None:
        host = urlsplit(url).hostname or ""
        if not host or (not allow_private and is_private_host(host)):
            return None
        target = rewrite(url) if rewrite is not None else None
        headers = {"x-forwarded-host": host} if target else {}
        try:
            resp = await context.request.get(
                target or url, headers=headers, timeout=5_000, max_redirects=3
            )
            if not resp.ok:
                return None
            body = await resp.body()
        except PlaywrightError:
            return None
        return body if 0 < len(body) <= max_bytes else None

    return fetch


async def screenshot_jpeg(page: Page, max_bytes: int) -> bytes:
    """Viewport JPEG under the size cap: lower quality step by step (FR-CW-08)."""
    for quality in (70, 55, 40, 30, 20, 10):
        try:
            data = await page.screenshot(
                type="jpeg", quality=quality, full_page=False, timeout=10_000
            )
        except PlaywrightError:
            return b""
        if len(data) <= max_bytes:
            return data
    return data[:0]  # even the lowest quality is too big: no screenshot rather than an oversize one


async def capture_page(
    page: Page,
    *,
    page_type: str,
    global_candidates: list[str],
    cookie_names: list[str],
    headers: dict[str, str],
    screenshot_max_bytes: int,
    favicon_fetch: FaviconFetcher | None = None,
) -> CheckoutCapture:
    cap = CheckoutCapture(url=page.url, page_type=page_type)
    try:
        cap.title = await page.title()
        cap.raw_html = await page.content()
    except PlaywrightError:
        return cap
    cap.features = extract(cap.raw_html, page.url)
    cap.clean_html, cap.sanitize = sanitize_text(cap.raw_html)
    cap.headers, _ = sanitize_headers(headers)
    cap.cookie_names = cookie_names
    try:
        cap.body_text = str(
            await page.evaluate("() => (document.body && document.body.innerText) || ''")
        )[:50_000]
    except PlaywrightError:
        cap.body_text = cap.features.text
    for frame in page.frames:
        try:
            found = await frame.evaluate(_GLOBALS_JS, global_candidates)
        except PlaywrightError:
            continue
        cap.js_globals.update(str(g) for g in found)
    try:
        labels = await page.evaluate(_LABELS_JS, list(PAYMENT_BLOCK_SELECTORS))
        cap.labels = [str(x) for x in labels][:MAX_LABELS]
    except PlaywrightError:
        cap.labels = []
    try:
        block = await page.evaluate(_PAYMENT_BLOCK_JS, list(PAYMENT_BLOCK_SELECTORS))
    except PlaywrightError:
        block = None
    if block:
        html = str(block.get("html", ""))[:MAX_DOM_BYTES]
        cap.payment_block_html, _ = sanitize_text(html)
        cap.payment_block_selector = str(block.get("selector", ""))
    try:
        cap.favicon_url = str(await page.evaluate(_FAVICON_JS))[:1024]
    except PlaywrightError:
        cap.favicon_url = ""
    if cap.favicon_url and favicon_fetch is not None:
        data = await favicon_fetch(cap.favicon_url)
        if data:
            cap.favicon_sha256 = hashlib.sha256(data).hexdigest()
    cap.screenshot_jpeg = await screenshot_jpeg(page, screenshot_max_bytes)
    m = re.search(
        r"[\d.,]+\s?(?:€|EUR|£|GBP|\$|USD|CHF|PLN|zł|SEK|DKK|NOK|kr)|(?:€|EUR|£|GBP|\$|USD|CHF|PLN|SEK|DKK|NOK)\s?[\d.,]+",
        cap.body_text,
    )
    cap.currency_text_sample = m.group(0) if m else ""
    return cap


def network_rows(recorder: NetworkRecorder) -> list[dict[str, Any]]:
    """HAR-like compact list for the manifest (bodies are never kept)."""
    return [
        {
            "url": e.url,
            "method": e.method,
            "type": e.resource_type,
            "initiator": e.initiator,
            "status": e.status,
            "host": e.host,
        }
        for e in recorder.entries
    ]
