"""Loader for technology signatures in the Wappalyzer / webappanalyzer format (FR-LS-05).

The format is a JSON object `{"Technology name": {...}}` whose fields describe
where a technology shows up: `scriptSrc`, `js`, `headers`, `cookies`, `html`,
`meta` (and a few we cannot evaluate from a stored scan: `dom`, `css`, `xhr`,
`url`, `dns`, `robots`, `certIssuer`, `probe`). Patterns are JavaScript regexes
with `\\;version:…` / `\\;confidence:…` tags appended.

`convert()` turns such a file into rule documents of our own YAML format
(`rules/schema/rule.schema.json`): a technology whose name matches a platform of
the reference becomes `platform:<id>`, everything else `tech:<slug>`. Every
imported rule is written `enabled: false, needs_verification: true` (brief rule 8):
the licence of the data set is not cleared (ADR-0005) and nothing from it is used
in detection until a person flips the rule after a check.

No Wappalyzer data ships with the repository: `payintel rules-import-wappalyzer`
converts a file the operator supplies.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from payintel.core.errors import RuleError
from payintel.core.reference_loader import ReferenceData
from payintel.detect.rules import _schema_validator, parse_rule_file

UNSUPPORTED_FIELDS = ("dom", "css", "xhr", "url", "dns", "robots", "certIssuer", "probe", "text")
IGNORED_FIELDS = (
    "cats",
    "website",
    "description",
    "icon",
    "implies",
    "requires",
    "requiresCategory",
    "excludes",
    "cpe",
    "saas",
    "oss",
    "pricing",
)
_TAG_RE = re.compile(r"\\;(version|confidence):(.*?)(?=\\;|$)")
_SLUG_RE = re.compile(r"[^a-z0-9_]+")


@dataclass
class ConvertedRule:
    signal_type: str
    pattern: str
    match: str
    weight: float
    note: str


@dataclass
class ConvertedTechnology:
    name: str
    target_type: str  # platform | tech
    target_id: str
    website: str
    rules: list[ConvertedRule] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # unsupported fields / bad patterns

    def document(self, *, source: str) -> dict[str, Any]:
        rules = []
        counters: dict[str, int] = {}
        for r in self.rules:
            n = counters.get(r.signal_type, 0) + 1
            counters[r.signal_type] = n
            rules.append(
                {
                    "id": f"{self.target_type}.{self.target_id}.{r.signal_type}.{n}",
                    "version": 1,
                    "signal_type": r.signal_type,
                    "match": r.match,
                    "pattern": r.pattern,
                    "weight": r.weight,
                    "page_scope": "any",
                    "enabled": False,
                    "needs_verification": True,
                    "note": r.note,
                }
            )
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "source": source,
            "rules": rules,
        }


@dataclass
class ConvertResult:
    technologies: list[ConvertedTechnology]
    unsupported: dict[str, int]  # field name → occurrences
    invalid_patterns: int

    @property
    def rules(self) -> int:
        return sum(len(t.rules) for t in self.technologies)


def slug(name: str) -> str:
    s = _SLUG_RE.sub("_", name.lower()).strip("_")
    return (s or "tech")[:64]


def _platform_index(reference: ReferenceData | None) -> dict[str, str]:
    """Lower-cased platform names and aliases of the reference → platform id."""
    index: dict[str, str] = {}
    for p in reference.platforms if reference is not None else []:
        names = [p["name"], *(p.get("aliases") or [])]
        for n in names:
            index[str(n).lower()] = p["id"]
            # "Magento 2 / Adobe Commerce" → also "magento 2", "adobe commerce"
            for part in str(n).split("/"):
                index.setdefault(part.strip().lower(), p["id"])
    return index


def _strip_tags(pattern: str) -> tuple[str, float | None]:
    """Remove `\\;version:` / `\\;confidence:` tags; a confidence tag becomes the weight."""
    confidence: float | None = None
    for m in _TAG_RE.finditer(pattern):
        if m.group(1) == "confidence":
            try:
                confidence = max(0.0, min(1.0, float(m.group(2)) / 100.0))
            except ValueError:
                confidence = None
    return _TAG_RE.sub("", pattern), confidence


def _regex(pattern: str) -> str | None:
    """A Python-compilable, case-insensitive version of a Wappalyzer (JS) regex."""
    body = pattern.replace("(?<", "(?P<") if "(?<" in pattern and "(?<=" not in pattern else pattern
    rx = body if body.startswith("(?i)") else "(?i)" + body
    try:
        re.compile(rx)
    except re.error:
        return None
    return rx


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def convert_technology(
    name: str, spec: dict[str, Any], platforms: dict[str, str]
) -> ConvertedTechnology:
    target_id = platforms.get(name.lower())
    tech = ConvertedTechnology(
        name=name,
        target_type="platform" if target_id else "tech",
        target_id=target_id or slug(name),
        website=str(spec.get("website", "")),
    )
    base_weight = 0.5

    def add(signal: str, pattern: str, match: str, weight: float | None, note: str) -> None:
        tech.rules.append(
            ConvertedRule(
                signal, pattern, match, weight if weight is not None else base_weight, note
            )
        )

    for key in ("scriptSrc", "scripts"):
        for raw in _as_list(spec.get(key)):
            pat, conf = _strip_tags(raw)
            rx = _regex(pat)
            if rx is None:
                tech.skipped.append(f"{key}: invalid regex {raw!r}")
                continue
            add("script_src", rx, "regex", conf, f"wappalyzer {key}")
    for js_name, _value in (spec.get("js") or {}).items():
        top = str(js_name).split(".")[0].split("[")[0].strip()
        if not top:
            continue
        add("js_global", top, "exact", None, f"wappalyzer js {js_name}")
    for header, raw in (spec.get("headers") or {}).items():
        pat, conf = _strip_tags(str(raw or ""))
        value_rx = _regex(pat) if pat else None
        if pat and value_rx is None:
            tech.skipped.append(f"headers: invalid regex {raw!r}")
            continue
        pattern = f"{str(header).lower()}: {value_rx}" if value_rx else str(header).lower()
        add("header", pattern, "header_name", conf, f"wappalyzer header {header}")
    for cookie, _raw in (spec.get("cookies") or {}).items():
        add("cookie", str(cookie), "cookie_name", None, f"wappalyzer cookie {cookie}")
    for raw in _as_list(spec.get("html")):
        pat, conf = _strip_tags(raw)
        rx = _regex(pat)
        if rx is None:
            tech.skipped.append(f"html: invalid regex {raw!r}")
            continue
        add("html_pattern", rx, "regex", conf, "wappalyzer html")
    for meta_name, raw in (spec.get("meta") or {}).items():
        for one in _as_list(raw):
            pat, conf = _strip_tags(one)
            value_rx = _regex(pat) if pat else "(?i)"
            if value_rx is None:
                tech.skipped.append(f"meta: invalid regex {one!r}")
                continue
            body = value_rx[4:] if value_rx.startswith("(?i)") else value_rx
            name_rx = re.escape(str(meta_name))
            rx = (
                rf'(?i)<meta[^>]+name=["\']?{name_rx}["\']?[^>]+content=["\'][^"\']*{body}'
                rf'|<meta[^>]+content=["\'][^"\']*{body}[^"\']*["\'][^>]+name=["\']?{name_rx}'
            )
            if _regex(rx) is None:
                tech.skipped.append(f"meta: invalid regex {one!r}")
                continue
            add("html_pattern", rx, "regex", conf, f"wappalyzer meta {meta_name}")
    for key in UNSUPPORTED_FIELDS:
        if key in spec:
            tech.skipped.append(f"{key}: not evaluable from a stored scan")
    return tech


def convert(technologies: dict[str, Any], *, reference: ReferenceData | None) -> ConvertResult:
    platforms = _platform_index(reference)
    out: list[ConvertedTechnology] = []
    unsupported: dict[str, int] = {}
    invalid = 0
    for name, spec in technologies.items():
        if not isinstance(spec, dict):
            continue
        tech = convert_technology(str(name), spec, platforms)
        for s in tech.skipped:
            key = s.split(":", 1)[0]
            if key in UNSUPPORTED_FIELDS:
                unsupported[key] = unsupported.get(key, 0) + 1
            else:
                invalid += 1
        if tech.rules:
            out.append(tech)
    return ConvertResult(out, unsupported, invalid)


def load_technologies(paths: Iterable[Path]) -> dict[str, Any]:
    """Merge one or more technology JSON files (webappanalyzer splits them by letter)."""
    merged: dict[str, Any] = {}
    for path in paths:
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            raise RuleError(f"{path.name}: top level must be an object of technologies")
        merged.update(doc)
    return merged


def write_rule_files(
    result: ConvertResult, out_dir: Path, *, source: str, reference: ReferenceData | None
) -> list[Path]:
    """One YAML per technology (several technologies mapping to one platform are merged),
    validated by the regular rule parser before the function returns."""
    out_dir.mkdir(parents=True, exist_ok=True)
    docs: dict[str, dict[str, Any]] = {}
    for tech in result.technologies:
        doc = tech.document(source=f"{source} ({tech.name}; {tech.website})".strip())
        key = f"{tech.target_type}.{tech.target_id}"
        if key in docs:
            existing = docs[key]["rules"]
            offset = len(existing)
            for i, r in enumerate(doc["rules"], start=offset + 1):
                r["id"] = re.sub(r"\.(\d+)$", f".{i}", r["id"])
            existing.extend(doc["rules"])
            docs[key]["source"] += f"; {tech.name}"
        else:
            docs[key] = doc
    written: list[Path] = []
    validator = _schema_validator()
    for key, doc in sorted(docs.items()):
        path = out_dir / f"{key.split('.', 1)[1]}.yaml"
        header = (
            f"# Imported from a Wappalyzer-format file (FR-LS-05, ADR-0005): {doc['source']}\n"
            "# Every rule stays disabled with needs_verification: true until a person checks\n"
            "# the signature and the licence of the data set.\n"
        )
        path.write_text(
            header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=1000),
            encoding="utf-8",
        )
        parse_rule_file(path, reference=reference, validator=validator)
        written.append(path)
    return written
