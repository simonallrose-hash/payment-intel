"""Store country and currency from weighted signals (FR-DT-09).

Signals: ccTLD, currency on the page, `<html lang>` and hreflang, schema.org
PostalAddress.addressCountry, international phone prefixes in the raw page
text (before PII removal, never stored), hosting-IP country (low weight,
only when the host row already carries it). The result is an ISO 3166-1
alpha-2 code with a confidence level derived from the winner's share of votes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from payintel.core.models.base import ConfidenceLevel
from payintel.core.reference_loader import REFERENCE_DIR, validate_schema
from payintel.crawl.light.html import HtmlFeatures

_PHONE_PREFIX_RE = re.compile(r"(?:\+|\b00)(\d{1,4})[\s.\-(]")
_CODE_RE = re.compile(
    r"\b(EUR|USD|GBP|CHF|PLN|CZK|SEK|NOK|DKK|HUF|RON|BGN|CAD|AUD|NZD|JPY|BRL|MXN|INR|TRY|UAH|ILS|AED|SGD|ZAR)\b"
)


@dataclass
class CountryResult:
    country: str | None
    confidence: ConfidenceLevel | None
    currency: str | None
    votes: dict[str, float] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)


class CountryDetector:
    def __init__(self, path: Path = REFERENCE_DIR / "country_signals.yaml") -> None:
        with path.open("r", encoding="utf-8") as fh:
            doc: dict[str, Any] = yaml.safe_load(fh)
        validate_schema(doc, "country_signals.schema.json", label=path.name)
        self.w: dict[str, float] = {k: float(v) for k, v in doc["weights"].items()}
        self.cctld: dict[str, str] = {k.lower(): v for k, v in doc["cctld"].items()}
        self.currency: dict[str, list[str]] = doc["currency"]
        self.symbols: dict[str, str] = doc["currency_symbols"]
        self.lang: dict[str, list[str]] = {k.lower(): v for k, v in doc["lang"].items()}
        self.phone: dict[str, str] = doc["phone"]
        self.names: dict[str, str] = {k.lower(): v for k, v in doc["country_names"].items()}
        alts = "|".join(map(re.escape, sorted(self.symbols, key=len, reverse=True)))
        self._symbol_re = re.compile(rf"(?:(?:{alts})\s?\d|\d\s?(?:{alts}))")

    # --- individual signals -------------------------------------------------
    def currency_of(self, text: str) -> str | None:
        counts: dict[str, int] = {}
        for m in _CODE_RE.finditer(text):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        for m in self._symbol_re.finditer(text):
            for sym, code in self.symbols.items():
                if sym in m.group(0):
                    counts[code] = counts.get(code, 0) + 1
                    break
        if not counts:
            return None
        return max(sorted(counts), key=lambda c: counts[c])

    def _lang_votes(
        self, tag: str, weight: float, votes: dict[str, float], signals: list[str], label: str
    ) -> None:
        tag = tag.lower().replace("_", "-")
        if not tag or tag == "x-default":
            return
        parts = tag.split("-")
        region = next((p.upper() for p in parts[1:] if len(p) == 2 and p.isalpha()), None)
        if region:
            votes[region] = votes.get(region, 0.0) + weight
            signals.append(f"{label}:{tag}")
            return
        countries = self.lang.get(parts[0], [])
        if countries:
            share = weight / len(countries)
            for c in countries:
                votes[c] = votes.get(c, 0.0) + share
            signals.append(f"{label}:{parts[0]}")

    def _schema_country(self, blocks: list[dict[str, Any]]) -> str | None:
        def walk(node: Any, depth: int = 0) -> str | None:
            if depth > 6:
                return None
            if isinstance(node, dict):
                ac = node.get("addressCountry")
                if isinstance(ac, dict):
                    ac = ac.get("name")
                if isinstance(ac, str):
                    v = ac.strip()
                    if len(v) == 2 and v.isalpha():
                        return v.upper()
                    return self.names.get(v.lower())
                for v in node.values():
                    found = walk(v, depth + 1)
                    if found:
                        return found
            elif isinstance(node, list):
                for v in node:
                    found = walk(v, depth + 1)
                    if found:
                        return found
            return None

        for b in blocks:
            found = walk(b)
            if found:
                return found
        return None

    def _phone_country(self, raw_text: str) -> str | None:
        counts: dict[str, int] = {}
        for m in _PHONE_PREFIX_RE.finditer(raw_text):
            digits = m.group(1)
            for n in (4, 3, 2, 1):
                code = self.phone.get(digits[:n])
                if code:
                    counts[code] = counts.get(code, 0) + 1
                    break
        if not counts:
            return None
        return max(sorted(counts), key=lambda c: counts[c])

    # --- combination --------------------------------------------------------
    def detect(
        self,
        features: HtmlFeatures,
        *,
        etld1: str,
        raw_text: str | None = None,
        hosting_country: str | None = None,
    ) -> CountryResult:
        votes: dict[str, float] = {}
        signals: list[str] = []
        tld = etld1.rsplit(".", 1)[-1].lower()
        if tld in self.cctld:
            c = self.cctld[tld]
            votes[c] = votes.get(c, 0.0) + self.w["cctld"]
            signals.append(f"cctld:{tld}")
        currency = self.currency_of(features.text)
        if currency and currency in self.currency:
            countries = self.currency[currency]
            share = self.w["currency"] / len(countries)
            for c in countries:
                votes[c] = votes.get(c, 0.0) + share
            signals.append(f"currency:{currency}")
        if features.lang:
            self._lang_votes(features.lang, self.w["lang"], votes, signals, "lang")
        regions = [h for h in features.hreflangs if "-" in h]
        for h in regions[:5]:
            self._lang_votes(
                h, self.w["hreflang"] / max(1, len(regions)), votes, signals, "hreflang"
            )
        sc = self._schema_country(features.json_ld_blocks)
        if sc:
            votes[sc] = votes.get(sc, 0.0) + self.w["schema_address"]
            signals.append(f"schema_address:{sc}")
        pc = self._phone_country(raw_text or features.text)
        if pc:
            votes[pc] = votes.get(pc, 0.0) + self.w["phone"]
            signals.append(f"phone:{pc}")
        if hosting_country:
            hc = hosting_country.upper()
            votes[hc] = votes.get(hc, 0.0) + self.w["hosting_ip"]
            signals.append(f"hosting_ip:{hc}")
        if not votes:
            return CountryResult(None, None, currency, {}, signals)
        total = sum(votes.values())
        ranked = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
        winner, top = ranked[0]
        runner = ranked[1][1] if len(ranked) > 1 else 0.0
        share = top / total
        if top >= 0.5 and share >= 0.6:
            conf = ConfidenceLevel.HIGH
        elif top >= 0.2 and top - runner >= 0.1:
            conf = ConfidenceLevel.MEDIUM
        else:
            conf = ConfidenceLevel.LOW
        return CountryResult(
            winner, conf, currency, {k: round(v, 3) for k, v in votes.items()}, signals
        )


@lru_cache(maxsize=1)
def get_country_detector() -> CountryDetector:
    return CountryDetector()
