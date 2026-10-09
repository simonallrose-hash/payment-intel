"""Store vertical from menu categories and schema.org markup (FR-DT-10).

A dictionary classifier over the simplified 20-vertical taxonomy in
`reference/verticals.yaml`. Keyword hits are counted in four sources with
different weights: menu link paths (`/collections/shoes`), schema.org
`category` / breadcrumb / item names, title + meta description/keywords, and
the visible text (capped so a long page cannot drown the menu). The winner's
share of all votes gives the confidence level; with no hit at all the vertical
stays unknown rather than defaulting to `other`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

from payintel.core.models.base import ConfidenceLevel
from payintel.core.reference_loader import REFERENCE_DIR, validate_schema
from payintel.crawl.light.html import HtmlFeatures

W_MENU = 2.0
W_SCHEMA = 3.0
W_META = 2.0
W_TEXT = 1.0
TEXT_CAP = 5
MENU_CAP = 8
_SCHEMA_KEYS = ("category", "name", "description", "keywords", "about")
_LIST_TYPES = {"BreadcrumbList", "ItemList", "SiteNavigationElement", "Product", "Offer"}
_SPLIT = re.compile(r"[^\w]+", re.UNICODE)


@dataclass
class VerticalResult:
    vertical_id: str | None
    confidence: ConfidenceLevel | None
    votes: dict[str, float] = field(default_factory=dict)
    signals: list[str] = field(default_factory=list)


class VerticalDetector:
    def __init__(self, path: Path = REFERENCE_DIR / "verticals.yaml") -> None:
        with path.open("r", encoding="utf-8") as fh:
            doc: dict[str, Any] = yaml.safe_load(fh)
        validate_schema(doc, "verticals.schema.json", label=path.name)
        self.keywords: dict[str, list[str]] = {}
        self._patterns: dict[str, re.Pattern[str]] = {}
        for v in doc["verticals"]:
            kws = sorted({str(k).strip().lower() for k in v.get("keywords", []) if str(k).strip()})
            if not kws:
                continue
            self.keywords[v["id"]] = kws
            alts = "|".join(re.escape(k).replace(r"\ ", r"[\s_\-]+") for k in kws)
            self._patterns[v["id"]] = re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.UNICODE)

    # --- sources ----------------------------------------------------------------
    @staticmethod
    def menu_text(features: HtmlFeatures) -> str:
        """Path segments of internal links, one line per link, decoded and tokenised."""
        own = (urlsplit(features.base_url).hostname or "").lower()
        lines: list[str] = []
        seen: set[str] = set()
        for url in features.links:
            parts = urlsplit(url)
            host = (parts.hostname or own).lower()
            if host != own:
                continue
            path = unquote(parts.path).lower()
            if not path or path == "/" or path in seen:
                continue
            seen.add(path)
            lines.append(" ".join(t for t in _SPLIT.split(path) if t))
        return "\n".join(lines)

    @staticmethod
    def schema_text(features: HtmlFeatures) -> str:
        chunks: list[str] = []

        def walk(node: Any, depth: int = 0) -> None:
            if depth > 6:
                return
            if isinstance(node, dict):
                for key in _SCHEMA_KEYS:
                    val = node.get(key)
                    if isinstance(val, str):
                        chunks.append(val)
                    elif isinstance(val, list):
                        chunks.extend(x for x in val if isinstance(x, str))
                for v in node.values():
                    walk(v, depth + 1)
            elif isinstance(node, list):
                for v in node:
                    walk(v, depth + 1)

        for block in features.json_ld_blocks:
            walk(block)
        return "\n".join(chunks).lower()

    @staticmethod
    def meta_text(features: HtmlFeatures) -> str:
        m = features.meta
        return " ".join(
            x
            for x in (
                features.title,
                m.get("description", ""),
                m.get("keywords", ""),
                m.get("og:title", ""),
                m.get("og:description", ""),
            )
            if x
        ).lower()

    def _hits(self, text: str) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        if not text:
            return out
        for vid, rx in self._patterns.items():
            found = {m.group(0).lower() for m in rx.finditer(text)}
            if found:
                out[vid] = found
        return out

    # --- detection ----------------------------------------------------------------
    def detect(self, home: HtmlFeatures, *extra: HtmlFeatures) -> VerticalResult:
        pages = [home, *extra]
        votes: dict[str, float] = {}
        signals: list[str] = []
        sources: list[tuple[str, str, float, int | None]] = [
            ("menu", "\n".join(self.menu_text(p) for p in pages), W_MENU, MENU_CAP),
            ("schema", "\n".join(self.schema_text(p) for p in pages), W_SCHEMA, None),
            ("meta", " ".join(self.meta_text(p) for p in pages), W_META, None),
            ("text", " ".join(p.text.lower() for p in pages), W_TEXT, TEXT_CAP),
        ]
        for label, text, weight, cap in sources:
            for vid, found in self._hits(text).items():
                n = len(found) if cap is None else min(len(found), cap)
                votes[vid] = votes.get(vid, 0.0) + weight * n
                signals.append(f"{label}:{vid}:" + ",".join(sorted(found)[:5]))
        if not votes:
            return VerticalResult(None, None, {}, [])
        winner = max(sorted(votes), key=lambda v: votes[v])
        total = sum(votes.values())
        share = votes[winner] / total
        if share >= 0.6 and votes[winner] >= 6:
            level = ConfidenceLevel.HIGH
        elif share >= 0.4 and votes[winner] >= 3:
            level = ConfidenceLevel.MEDIUM
        else:
            level = ConfidenceLevel.LOW
        return VerticalResult(winner, level, dict(sorted(votes.items())), signals)
