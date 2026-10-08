"""Browser-side guardrails (FR-CW-04): JavaScript injected into every page and request routing.

Three independent layers sit under the Python `ClickPolicy`:

1. `DESCRIBE_CONTROL_JS` — read-only: turns an element into the JSON the policy
   decides on (text, aria-label, value, attributes, enclosing form).
2. `init_script(...)` — installed with `context.add_init_script`, so it runs
   before any page script: a capturing `submit` listener cancels submissions of
   order/payment forms (and *every* form once the walk switched to the capture
   phase), and a capturing `click` listener cancels clicks on final-action
   controls even if some code path tried to dispatch them. Blocked attempts are
   collected in `window.__payintel.blocked` and end in the walk journal.
3. `route_handler(...)` — `context.route("**/*")`: aborts POST/PUT/PATCH
   requests whose URL carries an order marker, refuses private / loopback
   targets (egress, NFR-S-09) and, in tests only, rewrites URLs to the local
   fixture server.

None of this hides the browser or alters its fingerprint (AS-20): the script
only adds event listeners.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Request, Route

from payintel.crawl.checkout.dictionary import CheckoutDictionary

PHASE_WALK = "walk"
PHASE_CAPTURE = "capture"
BLOCKED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PRIVATE_SUFFIXES = (".local", ".internal", ".localhost", ".localdomain", ".home.arpa")

DESCRIBE_CONTROL_JS = r"""
(el) => {
  const attr = (e, n) => (e && e.getAttribute && e.getAttribute(n)) || "";
  const txt = (el.innerText || el.textContent || "").trim().slice(0, 300);
  const form = el.closest ? el.closest("form") : null;
  const data = [];
  if (el.attributes) {
    for (const a of el.attributes) {
      if (a.name.startsWith("data-")) data.push(a.name.slice(5) + "=" + String(a.value).slice(0, 80));
    }
  }
  let imgAlt = "";
  if (el.querySelector) { const img = el.querySelector("img[alt]"); if (img) imgAlt = img.getAttribute("alt") || ""; }
  return {
    tag: (el.tagName || "").toLowerCase(),
    type: (attr(el, "type") || (el.tagName === "BUTTON" ? "submit" : "")).toLowerCase(),
    role: attr(el, "role"),
    text: txt,
    ariaLabel: attr(el, "aria-label"),
    value: (el.tagName === "INPUT" ? (el.value || "") : "").slice(0, 300),
    title: attr(el, "title"),
    alt: attr(el, "alt") || imgAlt,
    name: attr(el, "name"),
    id: el.id || "",
    classes: (typeof el.className === "string" ? el.className : "") || "",
    data: data.join(" "),
    href: attr(el, "href") || attr(el, "formaction"),
    formAction: form ? (attr(form, "action") || "") : "",
    formId: form ? (form.id || "") : "",
    formClasses: form ? ((typeof form.className === "string" ? form.className : "") || "") : "",
    formName: form ? (attr(form, "name") || "") : "",
    disabled: !!(el.disabled || attr(el, "aria-disabled") === "true"),
  };
}
"""

CLICKABLE_SELECTOR = (
    "button, a[href], input[type=submit], input[type=button], input[type=image], "
    "[role=button], [onclick]"
)


def init_script(d: CheckoutDictionary) -> str:
    """The guard installed on every page (see module docstring)."""
    cfg = {
        "final": sorted(d.phrases("final_actions")),
        "brands": sorted(d.final_brands),
        "markers": sorted(d.final_markers),
        "formMarkers": sorted(d.order_form_markers),
    }
    return (
        "(() => {\n"
        f"const CFG = {json.dumps(cfg, ensure_ascii=False)};\n"
        + r"""
// Re-runs on every new document of the same window (e.g. Page.setDocumentContent)
// must not throw: reuse the state object when it is already installed.
const S = window.__payintel || { phase: "walk", blocked: [], installs: 0 };
if (!window.__payintel) Object.defineProperty(window, "__payintel", { value: S, writable: false, configurable: false });
const norm = (t) => (t || "").normalize("NFKC").toLowerCase().replace(/ /g, " ")
  .replace(/_/g, " ").replace(/[^\p{L}\p{N}\s]/gu, " ").replace(/\s+/g, " ").trim();
const has = (text, phrase) => (" " + text + " ").includes(" " + phrase + " ");
const labelsOf = (el) => {
  const a = (n) => (el.getAttribute && el.getAttribute(n)) || "";
  const img = el.querySelector ? el.querySelector("img[alt]") : null;
  return [el.innerText || el.textContent || "", a("aria-label"), el.value || "", a("title"), a("alt"),
          img ? img.getAttribute("alt") : ""].filter(Boolean).map(norm);
};
const attrsOf = (el) => {
  const a = (n) => ((el.getAttribute && el.getAttribute(n)) || "").toLowerCase();
  const out = [a("name"), el.id ? String(el.id).toLowerCase() : "",
               typeof el.className === "string" ? el.className.toLowerCase() : "", a("href"), a("formaction")];
  if (el.attributes) for (const at of el.attributes) if (at.name.startsWith("data-")) out.push(String(at.value).toLowerCase());
  return out;
};
const isFinalControl = (el) => {
  for (const l of labelsOf(el)) {
    for (const p of CFG.final) if (has(l, p)) return "phrase:" + p;
    for (const b of CFG.brands) if (has(l, b)) return "brand:" + b;
  }
  for (const v of attrsOf(el)) for (const m of CFG.markers) if (v.includes(m)) return "marker:" + m;
  return null;
};
const isOrderForm = (form) => {
  if (!form) return null;
  const vals = [(form.getAttribute("action") || ""), form.id || "",
                typeof form.className === "string" ? form.className : "", form.getAttribute("name") || ""]
                .map((v) => String(v).toLowerCase());
  for (const v of vals) for (const m of CFG.formMarkers) if (v.includes(m)) return "form:" + m;
  return null;
};
S.isFinal = isFinalControl;
S.isOrderForm = isOrderForm;
const record = (kind, why, el) => {
  try {
    S.blocked.push({ kind, why, tag: el && el.tagName ? el.tagName.toLowerCase() : "",
      text: el ? norm(el.innerText || el.textContent || el.value || "").slice(0, 120) : "", at: Date.now() });
  } catch (e) {}
};
const onSubmit = (ev) => {
  const form = ev.target;
  const submitter = ev.submitter || null;
  let why = null;
  if (S.phase === "capture") why = "phase:capture";
  else why = isOrderForm(form) || (submitter ? isFinalControl(submitter) : null);
  if (why) { ev.preventDefault(); ev.stopImmediatePropagation(); record("submit", why, submitter || form); }
};
const onClick = (ev) => {
  const el = ev.target && ev.target.closest ? ev.target.closest("button, a, input, [role=button]") : null;
  if (!el) return;
  const why = isFinalControl(el);
  if (why) { ev.preventDefault(); ev.stopImmediatePropagation(); record("click", why, el); }
};
const install = () => {
  window.removeEventListener("submit", onSubmit, true);
  window.removeEventListener("click", onClick, true);
  window.addEventListener("submit", onSubmit, true);
  window.addEventListener("click", onClick, true);
  S.installs += 1;
};
install();
// document.open() drops every listener on window and document; re-install after it.
if (!Document.prototype.__payintelPatched) {
Object.defineProperty(Document.prototype, "__payintelPatched", { value: true });
const nativeOpen = Document.prototype.open;
Document.prototype.open = function (...args) {
  const r = nativeOpen.apply(this, args);
  try { install(); } catch (e) {}
  return r;
};
}
if (!HTMLFormElement.prototype.__payintelPatched) {
Object.defineProperty(HTMLFormElement.prototype, "__payintelPatched", { value: true });
// The native form.submit() bypasses submit events: wrap it so order forms cannot be
// submitted programmatically either (last line of defence, FR-CW-04).
const nativeSubmit = HTMLFormElement.prototype.submit;
HTMLFormElement.prototype.submit = function () {
  const why = S.phase === "capture" ? "phase:capture" : isOrderForm(this);
  if (why) { record("submit()", why, this); return; }
  return nativeSubmit.call(this);
};
const nativeRequestSubmit = HTMLFormElement.prototype.requestSubmit;
if (nativeRequestSubmit) {
  HTMLFormElement.prototype.requestSubmit = function (submitter) {
    const why = S.phase === "capture" ? "phase:capture" : (isOrderForm(this) || (submitter ? isFinalControl(submitter) : null));
    if (why) { record("requestSubmit()", why, submitter || this); return; }
    return nativeRequestSubmit.call(this, submitter);
  };
}
}
})();
"""
    )


def is_private_host(hostname: str) -> bool:
    h = hostname.lower().rstrip(".")
    internal_names = {"localhost", "0.0.0.0", "metadata.google.internal"}  # noqa: S104
    if h in internal_names or h.endswith(PRIVATE_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
    except ValueError:
        return "." not in h  # single-label names are internal names
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


@dataclass
class RouteStats:
    blocked_posts: list[str] = field(default_factory=list)
    refused_hosts: list[str] = field(default_factory=list)
    rewrite_errors: list[str] = field(default_factory=list)
    requests: int = 0


Rewriter = Callable[[str], str | None]


def route_handler(
    d: CheckoutDictionary,
    stats: RouteStats,
    *,
    allow_private: bool = False,
    rewrite: Rewriter | None = None,
) -> Callable[[Route, Request], Any]:
    """Build the `context.route` callback."""
    markers = d.order_form_markers

    async def handle(route: Route, request: Request) -> None:
        stats.requests += 1
        url = request.url
        host = urlsplit(url).hostname or ""
        if not allow_private and is_private_host(host):
            stats.refused_hosts.append(host)
            await route.abort("blockedbyclient")
            return
        if request.method.upper() in BLOCKED_METHODS:
            parts = urlsplit(url)
            low = (parts.path + ("?" + parts.query if parts.query else "")).lower()
            if any(m in low for m in markers):
                stats.blocked_posts.append(url[:300])
                await route.abort("blockedbyclient")
                return
        if rewrite is not None:
            target = rewrite(url)
            if target is not None:
                try:
                    response = await route.fetch(
                        url=target, headers={**request.headers, "x-forwarded-host": host}
                    )
                    await route.fulfill(response=response)
                except Exception as exc:  # any transport error is a refused fetch
                    stats.rewrite_errors.append(f"{url[:200]}: {exc}"[:400])
                    await route.abort("connectionrefused")
                return
        await route.continue_()

    return handle
