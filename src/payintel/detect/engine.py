"""Rule engine: page signals × rules → findings (FR-DT-01, FR-DT-02, FR-DT-07, FR-LS-05).

Works on artefacts only (6.5 decision 1): the scanner collects `PageSignals`,
the engine never touches the network, so re-detection on stored artefacts
(FR-DT-12) is the same call on the same inputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

from payintel.core.models.base import PageScope, SignalType
from payintel.detect.rules import Rule, RuleSet


@dataclass
class PageSignals:
    page_type: str  # homepage | product | cart | checkout
    url: str
    html: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    cookie_names: set[str] = field(default_factory=set)
    script_srcs: list[str] = field(default_factory=list)
    iframe_srcs: list[str] = field(default_factory=list)
    form_actions: list[str] = field(default_factory=list)
    network_hosts: set[str] = field(default_factory=set)
    js_globals: set[str] = field(default_factory=set)
    favicon_sha256: str | None = None
    checkout_labels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Finding:
    rule: Rule
    signal_value: str
    page_type: str
    page_url: str


_PAGE_SCOPE_OK = {
    PageScope.ANY: {"homepage", "product", "cart", "checkout", "payment_step"},
    PageScope.HOMEPAGE: {"homepage"},
    PageScope.PRODUCT: {"product"},
    PageScope.CART: {"cart"},
    PageScope.CHECKOUT: {"checkout", "payment_step"},
}


@lru_cache(maxsize=4096)
def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


def _host_matches(host: str, pattern: str) -> bool:
    host = host.lower().rstrip(".")
    if pattern.startswith("*."):
        suffix = pattern[2:].lower()
        return host == suffix or host.endswith("." + suffix)
    p = pattern.lower()
    return host == p or host.endswith("." + p)


def _hosts_of(urls: list[str]) -> list[str]:
    out = []
    for u in urls:
        h = urlsplit(
            u if "://" in u else "https:" + u if u.startswith("//") else "https://" + u
        ).hostname
        if h:
            out.append(h.lower())
    return out


def _match_one(rule: Rule, s: PageSignals) -> str | None:
    """Return the matched signal value, or None."""
    st, pat, kind = rule.signal_type, rule.pattern, rule.match
    if st == SignalType.SCRIPT_SRC or st == SignalType.PLATFORM_PLUGIN:
        for src in s.script_srcs:
            if kind == "url_contains" and pat.lower() in src.lower():
                return src
            if kind == "regex" and _rx(pat).search(src):
                return src
            if kind == "host_suffix" and any(_host_matches(h, pat) for h in _hosts_of([src])):
                return src
        return None
    if st == SignalType.NETWORK_HOST:
        for h in sorted(s.network_hosts | set(_hosts_of(s.script_srcs + s.iframe_srcs))):
            if _host_matches(h, pat) if kind == "host_suffix" else (pat.lower() in h):
                return h
        return None
    if st == SignalType.IFRAME_SRC:
        for src in s.iframe_srcs:
            hosts = _hosts_of([src])
            if (kind == "host_suffix" and any(_host_matches(h, pat) for h in hosts)) or (
                kind == "url_contains" and pat.lower() in src.lower()
            ):
                return src
        return None
    if st == SignalType.FORM_ACTION:
        for a in s.form_actions:
            hosts = _hosts_of([a])
            if (kind == "host_suffix" and any(_host_matches(h, pat) for h in hosts)) or (
                kind == "url_contains" and pat.lower() in a.lower()
            ):
                return a
        return None
    if st == SignalType.JS_GLOBAL:
        if pat in s.js_globals:
            return pat
        if "." in pat:
            return next((g for g in s.js_globals if g.startswith(pat + ".")), None)
        return None
    if st == SignalType.HTML_PATTERN:
        m = (
            _rx(pat).search(s.html)
            if kind == "regex"
            else (pat if pat.lower() in s.html.lower() else None)
        )
        if m is None:
            return None
        return (
            m
            if isinstance(m, str)
            else s.html[max(0, m.start() - 20) : m.end() + 20].replace("\n", " ")
        )
    if st == SignalType.HEADER:
        name, _, value_rx = pat.partition(":")
        value = s.headers.get(name.strip().lower())
        if value is None:
            return None
        if value_rx.strip() and not _rx(value_rx.strip()).search(value):
            return None
        return f"{name.strip().lower()}: {value[:120]}"
    if st == SignalType.COOKIE:
        for c in s.cookie_names:
            if c == pat or (kind == "regex" and _rx(pat).match(c)):
                return c
        return None
    if st == SignalType.FAVICON:
        return pat if s.favicon_sha256 == pat else None
    if st == SignalType.CHECKOUT_LABEL:
        for label in s.checkout_labels:
            if _rx(pat).search(label):
                return label
        return None


def match_page(ruleset: RuleSet, signals: PageSignals) -> list[Finding]:
    findings: list[Finding] = []
    for rule in ruleset.enabled():
        if signals.page_type not in _PAGE_SCOPE_OK[rule.page_scope]:
            continue
        value = _match_one(rule, signals)
        if value is not None:
            findings.append(
                Finding(
                    rule=rule,
                    signal_value=value[:500],
                    page_type=signals.page_type,
                    page_url=signals.url,
                )
            )
    return findings


def match_pages(ruleset: RuleSet, pages: list[PageSignals]) -> list[Finding]:
    out: list[Finding] = []
    for p in pages:
        out.extend(match_page(ruleset, p))
    return out
