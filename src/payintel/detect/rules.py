"""Detection rules: YAML files → validated `Rule` objects → `detection_rule` table (FR-DT-01).

Rule files live under `rules/{providers,methods,platforms}/*.yaml`; each file
targets one reference entity and lists signals (FR-DT-02). Validation rejects
unknown signal types, unknown targets (FR-NR-05), invalid regexes, weights
outside 0..1, duplicate id+version pairs, and `enabled: true` combined with
`needs_verification: true` (brief rule 8: unverified signatures never fire).
A rule whose content changed must get a new `version`; re-loading the same
id+version with a different pattern is an error.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.errors import RuleError
from payintel.core.models.base import PageScope, RuleTargetType, SignalType
from payintel.core.models.rules import DetectionRule
from payintel.core.reference_loader import ReferenceData

RULES_DIR = Path(__file__).resolve().parents[3] / "rules"
RULE_SCHEMA = RULES_DIR / "schema" / "rule.schema.json"
RULE_SUBDIRS = ("providers", "methods", "platforms")

MatchKind = str
MATCH_KINDS: frozenset[str] = frozenset(
    {"host_suffix", "url_contains", "regex", "exact", "header_name", "cookie_name", "sha256"}
)
_SIGNAL_DEFAULT_MATCH: dict[SignalType, MatchKind] = {
    SignalType.SCRIPT_SRC: "url_contains",
    SignalType.NETWORK_HOST: "host_suffix",
    SignalType.IFRAME_SRC: "host_suffix",
    SignalType.JS_GLOBAL: "exact",
    SignalType.HTML_PATTERN: "regex",
    SignalType.FORM_ACTION: "host_suffix",
    SignalType.CHECKOUT_LABEL: "regex",
    SignalType.PLATFORM_PLUGIN: "url_contains",
    SignalType.HEADER: "header_name",
    SignalType.COOKIE: "cookie_name",
    SignalType.FAVICON: "sha256",
}
_HOST_RE = re.compile(r"^(\*\.)?([a-z0-9-]+\.)+[a-z]{2,}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_JS_GLOBAL_RE = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: int
    target_type: RuleTargetType
    target_id: str
    signal_type: SignalType
    pattern: str
    match: MatchKind
    weight: float
    page_scope: PageScope
    enabled: bool
    needs_verification: bool
    source_file: str
    note: str = ""

    @property
    def fingerprint(self) -> str:
        payload = {
            "target_type": self.target_type.value,
            "target_id": self.target_id,
            "signal_type": self.signal_type.value,
            "pattern": self.pattern,
            "match": self.match,
            "weight": self.weight,
            "page_scope": self.page_scope.value,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class RuleSet:
    rules: tuple[Rule, ...]

    @property
    def version(self) -> str:
        """Content hash of all rules; recorded as `ruleset_version` on scans (FR-DT-07)."""
        h = hashlib.sha256()
        for r in sorted(self.rules, key=lambda r: (r.rule_id, r.version)):
            h.update(f"{r.rule_id}:{r.version}:{r.fingerprint}:{int(r.enabled)}".encode())
        return h.hexdigest()[:16]

    def enabled(self) -> list[Rule]:
        return [r for r in self.rules if r.enabled]

    def needing_verification(self) -> list[Rule]:
        return [r for r in self.rules if r.needs_verification]


def _schema_validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(RULE_SCHEMA.read_text(encoding="utf-8")))


def _validate_pattern(signal: SignalType, match: MatchKind, pattern: str, *, where: str) -> None:
    if match == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RuleError(f"{where}: invalid regex {pattern!r}: {exc}") from exc
        return
    if match == "host_suffix":
        if not _HOST_RE.match(pattern):
            raise RuleError(f"{where}: {pattern!r} is not a hostname or *.suffix")
        return
    if match == "sha256" or signal == SignalType.FAVICON:
        if not _SHA256_RE.match(pattern):
            raise RuleError(f"{where}: favicon pattern must be a lowercase sha256 hex")
        return
    if signal == SignalType.JS_GLOBAL and not _JS_GLOBAL_RE.match(pattern):
        raise RuleError(f"{where}: js_global pattern must be a dotted identifier")
    if not pattern.strip():
        raise RuleError(f"{where}: empty pattern")


def parse_rule_file(
    path: Path, *, reference: ReferenceData | None, validator: Draft202012Validator
) -> list[Rule]:
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise RuleError(f"{path.name}: top level must be a mapping")
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        msg = "; ".join(f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors[:5])
        raise RuleError(f"{path.name}: schema validation failed: {msg}")

    target_type = RuleTargetType(doc["target_type"])
    target_id = doc["target_id"]
    if reference is not None:
        known = {
            RuleTargetType.PROVIDER: {p["id"] for p in reference.providers},
            RuleTargetType.PAYMENT_METHOD: {m["id"] for m in reference.payment_methods},
            RuleTargetType.PLATFORM: {p["id"] for p in reference.platforms},
            RuleTargetType.TECH: None,
        }[target_type]
        if known is not None and target_id not in known:
            raise RuleError(
                f"{path.name}: target {target_type.value}:{target_id} not in reference (FR-NR-05)"
            )

    rules: list[Rule] = []
    for raw in doc["rules"]:
        where = f"{path.name}#{raw['id']}"
        signal = SignalType(raw["signal_type"])
        match = raw.get("match", _SIGNAL_DEFAULT_MATCH[signal])
        if match not in MATCH_KINDS:
            raise RuleError(f"{where}: unknown match kind {match}")
        weight = float(raw["weight"])
        if not 0.0 <= weight <= 1.0:
            raise RuleError(f"{where}: weight {weight} outside 0..1")
        enabled = bool(raw["enabled"])
        needs_verification = bool(raw.get("needs_verification", False))
        if enabled and needs_verification:
            raise RuleError(f"{where}: enabled rules cannot be needs_verification (brief rule 8)")
        expected_prefix = f"{target_type.value}.{target_id}."
        if not raw["id"].startswith(expected_prefix):
            raise RuleError(f"{where}: rule id must start with '{expected_prefix}'")
        _validate_pattern(signal, match, raw["pattern"], where=where)
        rules.append(
            Rule(
                rule_id=raw["id"],
                version=int(raw["version"]),
                target_type=target_type,
                target_id=target_id,
                signal_type=signal,
                pattern=raw["pattern"],
                match=match,
                weight=weight,
                page_scope=PageScope(raw.get("page_scope", "any")),
                enabled=enabled,
                needs_verification=needs_verification,
                source_file=str(path.relative_to(RULES_DIR))
                if path.is_relative_to(RULES_DIR)
                else path.name,
                note=raw.get("note", ""),
            )
        )
    return rules


def load_rules(
    directory: Path = RULES_DIR,
    *,
    reference: ReferenceData | None = None,
    files: Iterable[Path] | None = None,
) -> RuleSet:
    validator = _schema_validator()
    paths = (
        list(files)
        if files is not None
        else [p for sub in RULE_SUBDIRS for p in sorted((directory / sub).glob("*.yaml"))]
    )
    rules: list[Rule] = []
    for path in paths:
        rules.extend(parse_rule_file(path, reference=reference, validator=validator))
    seen: dict[tuple[str, int], Rule] = {}
    for r in rules:
        key = (r.rule_id, r.version)
        if key in seen:
            raise RuleError(
                f"duplicate rule {r.rule_id} v{r.version} "
                f"in {seen[key].source_file} and {r.source_file}"
            )
        seen[key] = r
    return RuleSet(rules=tuple(rules))


@dataclass
class RuleSyncResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def sync_rules(
    session: Session, ruleset: RuleSet, *, clock: Clock = SYSTEM_CLOCK
) -> RuleSyncResult:
    """Write rules to `detection_rule`. Same id+version with different content is rejected."""
    result = RuleSyncResult()
    existing_rows = {
        (row.rule_id, row.version): row for row in session.execute(select(DetectionRule)).scalars()
    }
    for r in ruleset.rules:
        row = existing_rows.get((r.rule_id, r.version))
        values: dict[str, Any] = {
            "target_type": r.target_type,
            "target_id": r.target_id,
            "signal_type": r.signal_type,
            "pattern": r.pattern,
            "weight": r.weight,
            "page_scope": r.page_scope,
            "enabled": r.enabled,
            "needs_verification": r.needs_verification,
            "source_file": r.source_file,
        }
        if row is None:
            session.add(
                DetectionRule(rule_id=r.rule_id, version=r.version, loaded_at=clock.now(), **values)
            )
            result.inserted += 1
            continue
        content_same = (
            row.pattern == r.pattern
            and row.signal_type == r.signal_type
            and row.target_id == r.target_id
            and row.target_type == r.target_type
            and abs(row.weight - r.weight) < 1e-9
            and row.page_scope == r.page_scope
        )
        if not content_same:
            raise RuleError(
                f"{r.rule_id} v{r.version} already loaded with different content; bump the version"
            )
        if (
            row.enabled == r.enabled
            and row.needs_verification == r.needs_verification
            and row.source_file == r.source_file
        ):
            result.unchanged += 1
            continue
        row.enabled = r.enabled
        row.needs_verification = r.needs_verification
        row.source_file = r.source_file
        row.loaded_at = clock.now()
        result.updated += 1
    session.flush()
    return result
