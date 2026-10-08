"""Checkout vocabulary in 10 languages (FR-CW-03, FR-CW-04): `reference/checkout_dictionary.yaml`.

The dictionary is data, the matching is here: text is normalised (NFKC,
case-folded, punctuation stripped, whitespace collapsed) and a phrase matches
when it equals the text or occurs in it as whole words. Attribute markers
(`final_markers`, `order_form_markers`) are plain substrings of lower-cased
attribute values.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from payintel.core.reference_loader import REFERENCE_DIR, validate_schema

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_LANG_SECTIONS = (
    "final_actions",
    "step_actions",
    "add_to_cart",
    "guest_checkout",
    "login",
    "register",
    "forbidden_consents",
    "required_consents",
    "shipping_words",
    "payment_words",
    "cart_words",
    "checkout_words",
    "out_of_stock",
    "price_on_request",
    "min_order_value",
    "email_verification",
    "sms_verification",
    "documents_required",
    "login_wall",
)


def normalize(text: str) -> str:
    """Canonical form used for every comparison (also by the trap-shop property test)."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = t.replace(" ", " ").replace("_", " ")
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def contains_phrase(text_norm: str, phrase_norm: str) -> bool:
    if not phrase_norm:
        return False
    return f" {phrase_norm} " in f" {text_norm} "


@dataclass(frozen=True)
class CheckoutDictionary:
    version: int
    languages: tuple[str, ...]
    sections: dict[str, tuple[str, ...]]  # section → normalised phrases, all languages merged
    by_language: dict[str, dict[str, tuple[str, ...]]]  # section → lang → phrases
    final_brands: tuple[str, ...]
    final_markers: tuple[str, ...]
    order_form_markers: tuple[str, ...]

    def phrases(self, section: str) -> tuple[str, ...]:
        return self.sections[section]

    def match(self, text: str, section: str) -> str | None:
        """Longest phrase of `section` contained in `text` (whole words), or None."""
        t = normalize(text)
        if not t:
            return None
        best: str | None = None
        for phrase in self.sections[section]:
            if contains_phrase(t, phrase) and (best is None or len(phrase) > len(best)):
                best = phrase
        return best

    def equals(self, text: str, section: str) -> bool:
        return normalize(text) in self.sections[section]

    def marker_in(self, attr_value: str, markers: tuple[str, ...]) -> str | None:
        v = (attr_value or "").lower()
        for m in markers:
            if m in v:
                return m
        return None


def _load(path: Path) -> CheckoutDictionary:
    with path.open("r", encoding="utf-8") as fh:
        doc: dict[str, Any] = yaml.safe_load(fh)
    validate_schema(doc, "checkout_dictionary.schema.json", label=path.name)
    languages = tuple(doc["languages"])
    sections: dict[str, tuple[str, ...]] = {}
    by_language: dict[str, dict[str, tuple[str, ...]]] = {}
    for section in _LANG_SECTIONS:
        per_lang = {
            lang: tuple(dict.fromkeys(normalize(p) for p in phrases))
            for lang, phrases in doc[section].items()
        }
        by_language[section] = per_lang
        merged = sorted({p for ps in per_lang.values() for p in ps if p}, key=len, reverse=True)
        sections[section] = tuple(merged)
    sections["final_brands"] = tuple(normalize(b) for b in doc["final_brands"])
    return CheckoutDictionary(
        version=int(doc["version"]),
        languages=languages,
        sections=sections,
        by_language=by_language,
        final_brands=sections["final_brands"],
        final_markers=tuple(m.lower() for m in doc["final_markers"]),
        order_form_markers=tuple(m.lower() for m in doc["order_form_markers"]),
    )


@lru_cache(maxsize=4)
def load_dictionary(path: Path = REFERENCE_DIR / "checkout_dictionary.yaml") -> CheckoutDictionary:
    return _load(path)
