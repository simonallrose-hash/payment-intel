"""E-commerce classifier on light-scan features (FR-DS-08).

Score = Σ weight·signal over: platform detected (with its confidence as a
multiplier), schema.org Product/Offer, add-to-cart wording, cart path,
checkout path, currency marker next to a number. Thresholds come from
`DiscoverySettings`; the verdict carries the score as its confidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from payintel.core.models.base import ConfidenceLevel, DomainStatus
from payintel.core.reference_loader import REFERENCE_DIR, validate_schema
from payintel.crawl.light.html import HtmlFeatures


@dataclass(frozen=True)
class ClassifierInput:
    features: HtmlFeatures
    platform_id: str | None = None
    platform_confidence: ConfidenceLevel | None = None


@dataclass
class Classification:
    score: float
    status: DomainStatus
    confidence: ConfidenceLevel
    signals: list[str] = field(default_factory=list)


_CONF_MULT = {ConfidenceLevel.HIGH: 1.0, ConfidenceLevel.MEDIUM: 0.7, ConfidenceLevel.LOW: 0.4}


class EcommerceClassifier:
    def __init__(
        self,
        path: Path = REFERENCE_DIR / "ecommerce_signals.yaml",
        *,
        ecommerce_threshold: float = 0.5,
        not_ecommerce_threshold: float = 0.15,
    ) -> None:
        with path.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        validate_schema(doc, "ecommerce_signals.schema.json", label=path.name)
        self.phrases: list[str] = [p.lower() for p in doc["add_to_cart_phrases"]]
        self.product_paths: list[str] = [p.lower() for p in doc["product_paths"]]
        self.cart_paths: list[str] = [p.lower() for p in doc["cart_paths"]]
        self.checkout_paths: list[str] = [p.lower() for p in doc["checkout_paths"]]
        self.schema_types: set[str] = set(doc["schema_org_types"])
        self.weights: dict[str, float] = {k: float(v) for k, v in doc["weights"].items()}
        markers = "|".join(re.escape(m) for m in doc["currency_markers"])
        self._price_re = re.compile(
            rf"(?:(?:{markers})\s?\d[\d.,]*|\d[\d.,]*\s?(?:{markers}))", re.I
        )
        self.ecommerce_threshold = ecommerce_threshold
        self.not_ecommerce_threshold = not_ecommerce_threshold

    def classify(self, inp: ClassifierInput) -> Classification:
        f = inp.features
        signals: list[str] = []
        score = 0.0
        if inp.platform_id:
            mult = _CONF_MULT[inp.platform_confidence or ConfidenceLevel.LOW]
            score += self.weights["platform"] * mult
            signals.append(f"platform:{inp.platform_id}")
        if self.schema_types & set(f.json_ld_types):
            score += self.weights["schema_org"]
            signals.append("schema_org")
        texts = set(f.button_texts)
        if any(any(p in t for p in self.phrases) for t in texts):
            score += self.weights["add_to_cart"]
            signals.append("add_to_cart")
        paths = f.link_paths() | {a.lower() for a in f.forms_action}
        if any(any(cp in p for cp in self.cart_paths) for p in paths):
            score += self.weights["cart_path"]
            signals.append("cart_path")
        if any(any(cp in p for cp in self.checkout_paths) for p in paths):
            score += self.weights["checkout_path"]
            signals.append("checkout_path")
        if self._price_re.search(f.text):
            score += self.weights["currency"]
            signals.append("currency")
        score = min(1.0, round(score, 3))
        if score >= self.ecommerce_threshold:
            status = DomainStatus.ECOMMERCE
        elif score < self.not_ecommerce_threshold:
            status = DomainStatus.NOT_ECOMMERCE
        else:
            status = DomainStatus.CANDIDATE
        if score >= 0.8 or score < 0.05:
            conf = ConfidenceLevel.HIGH
        elif score >= self.ecommerce_threshold or score < self.not_ecommerce_threshold:
            conf = ConfidenceLevel.MEDIUM
        else:
            conf = ConfidenceLevel.LOW
        return Classification(score=score, status=status, confidence=conf, signals=signals)


@lru_cache(maxsize=1)
def get_classifier() -> EcommerceClassifier:
    from payintel.core.settings import get_settings

    d = get_settings().discovery
    return EcommerceClassifier(
        ecommerce_threshold=d.ecommerce_threshold,
        not_ecommerce_threshold=d.not_ecommerce_threshold,
    )
