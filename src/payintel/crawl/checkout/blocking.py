"""Blocking and protection pages (FR-CW-06, AC-05): the walk stops, the host is `blocked`.

Only widely published challenge markers are used: Cloudflare ("Just a
moment…", `cf-challenge`), Akamai ("Access Denied" reference pages), DataDome
(`captcha-delivery.com`), PerimeterX / HUMAN (`px-captcha`), hCaptcha and
reCAPTCHA widgets, plus HTTP 403 / 429. A CAPTCHA that is merely embedded in
a long page (a login form) is not a block; it must dominate the page.
Nothing here tries to pass the challenge (LR-03).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "access denied",
    "security check",
    "verify you are human",
    "are you a robot",
    "bot verification",
    "pardon our interruption",
    "please verify",
    "checking your browser",
)
_MARKERS: tuple[tuple[str, str], ...] = (
    ("cloudflare", "cf-challenge"),
    ("cloudflare", "challenge-platform"),
    ("cloudflare", "cf_chl_opt"),
    ("cloudflare", "__cf_chl"),
    ("cloudflare", "cf-browser-verification"),
    ("cloudflare", "cf-error-details"),
    ("akamai", "errors.edgesuite.net"),
    ("akamai", "akamai.com/robots"),
    ("akamai", "bm-verify"),
    ("datadome", "captcha-delivery.com"),
    ("datadome", "datadome"),
    ("perimeterx", "px-captcha"),
    ("perimeterx", "perimeterx"),
    ("perimeterx", "_pxhd"),
    ("perimeterx", "press & hold"),
)
_CAPTCHA_MARKERS: tuple[tuple[str, str], ...] = (
    ("hcaptcha", "hcaptcha.com"),
    ("hcaptcha", "h-captcha"),
    ("recaptcha", "google.com/recaptcha"),
    ("recaptcha", "g-recaptcha"),
    ("recaptcha", "recaptcha/api.js"),
    ("turnstile", "challenges.cloudflare.com/turnstile"),
)
_GEO_RE = re.compile(
    r"not (?:available|accessible) in your (?:country|region|location)|"
    r"we (?:do not|don't) (?:ship|deliver) to your (?:country|region)|"
    r"nicht in ihrem land verf[üu]gbar|(?:non|pas) disponible dans votre pays|"
    r"no disponible en su pa[ií]s|non disponibile nel tuo paese|niet beschikbaar in uw land|"
    r"niedost[eę]pn[ya] w twoim kraju|n[ãa]o dispon[ií]vel no seu pa[ií]s",
    re.I,
)
_AGE_RE = re.compile(
    r"\b(?:are you (?:over|at least) (?:18|21)|confirm (?:that )?you(?:'re| are) (?:over|at least) (?:18|21)|"
    r"age verification|verify your age|altersverifikation|altersabfrage|sind sie (?:über|mindestens) 18|"
    r"vérification de l'âge|avez-vous plus de 18|verificación de edad|verifica dell'età|"
    r"leeftijdscontrole|weryfikacja wieku|verifica[çc][ãa]o de idade|åldersverifiering|aldersbekræftelse)\b",
    re.I,
)
SMALL_PAGE_CHARS = 2_500


@dataclass(frozen=True)
class BlockSignal:
    reason: str  # taxonomy code under `protection`
    blocked_by: str
    detail: str


def detect_block(
    *, status: int | None, title: str, html: str, text: str, url: str = ""
) -> BlockSignal | None:
    """Return the protection stop for a page, or None when the page is usable."""
    low_html = html.lower()
    low_title = (title or "").lower()
    low_text = (text or "").lower()
    small = len(low_text.strip()) < SMALL_PAGE_CHARS
    if status in {403, 429}:
        vendor = next((v for v, m in _MARKERS if m in low_html), None)
        return BlockSignal("http_403_429", vendor or f"http_{status}", f"HTTP {status} at {url}")
    if any(t in low_title for t in CHALLENGE_TITLES):
        vendor = next((v for v, m in _MARKERS if m in low_html), None)
        cap = next((v for v, m in _CAPTCHA_MARKERS if m in low_html), None)
        if cap and not vendor:
            return BlockSignal("captcha", cap, f"captcha page: {title[:80]}")
        return BlockSignal(
            "antibot_challenge", vendor or "unknown", f"challenge page: {title[:80]}"
        )
    for vendor, marker in _MARKERS:
        if marker in low_html and (small or status in {503, 403}):
            return BlockSignal(
                "antibot_challenge", vendor, f"marker {marker} on a {len(low_text)}-char page"
            )
    for vendor, marker in _CAPTCHA_MARKERS:
        if marker in low_html and small:
            return BlockSignal("captcha", vendor, f"captcha widget dominates the page ({marker})")
    if small and _GEO_RE.search(low_text):
        return BlockSignal("geo_blocked", "geo", _GEO_RE.search(low_text).group(0)[:80])  # type: ignore[union-attr]
    if _AGE_RE.search(low_text) and (small or _AGE_RE.search(low_title)):
        return BlockSignal("age_gate", "age_gate", _AGE_RE.search(low_text).group(0)[:80])  # type: ignore[union-attr]
    return None


def has_captcha_widget(html: str) -> str | None:
    """Name of a CAPTCHA widget present anywhere on the page (registration forms, FR-CW-12)."""
    low = html.lower()
    for vendor, marker in _CAPTCHA_MARKERS:
        if marker in low:
            return vendor
    return None
