"""Detection rules managed from the admin UI (FR-ADM-02, AS-18).

Rules live in YAML under `rules/` (loaded by `load_rules`) and, after `seed`,
in `detection_rule`. Operators change rules through this module only:

* every change is a **new version row** (`version + 1`, `source_file = "admin"`),
  the previous row stays for history (FR-DT-07 ruleset version changes);
* **delete** is "disable the current version" with an audit row (rules are
  never physically removed, like reference entries FR-NR-04);
* **preview** runs the candidate rule over the observed signals of gold-set
  hosts (ClickHouse `obs_*`) and compares firings with gold labels (FR-QA-01).

`overlay(session, ruleset)` applies the database state on top of the YAML
ruleset so the scanners pick up admin changes without a redeploy: for each
`rule_id` the highest database version wins, including its `enabled` flag.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import NotFoundError, RuleError, ValidationError
from payintel.core.models.base import (
    GoldEntityType,
    PageScope,
    RuleTargetType,
    SignalType,
)
from payintel.core.models.quality import GoldLabel
from payintel.core.models.rules import DetectionRule
from payintel.detect.engine import PageSignals, match_page
from payintel.detect.rules import (
    _SIGNAL_DEFAULT_MATCH,
    MATCH_KINDS,
    Rule,
    RuleSet,
    _validate_pattern,
)

ADMIN_SOURCE = "admin"

_GOLD_TYPE: dict[RuleTargetType, GoldEntityType | None] = {
    RuleTargetType.PROVIDER: GoldEntityType.PROVIDER,
    RuleTargetType.PAYMENT_METHOD: GoldEntityType.PAYMENT_METHOD,
    RuleTargetType.PLATFORM: GoldEntityType.PLATFORM,
    RuleTargetType.TECH: None,
}

# ClickHouse tables holding observed (signal_type, signal_value) per host.
_OBS_TABLES: tuple[str, ...] = ("obs_provider", "obs_payment_method", "obs_tech")


@dataclass(frozen=True)
class RuleDraft:
    rule_id: str
    target_type: RuleTargetType
    target_id: str
    signal_type: SignalType
    pattern: str
    weight: float
    page_scope: PageScope
    enabled: bool = True
    match: str | None = None
    note: str = ""

    def as_rule(self, version: int) -> Rule:
        return Rule(
            rule_id=self.rule_id,
            version=version,
            target_type=self.target_type,
            target_id=self.target_id,
            signal_type=self.signal_type,
            pattern=self.pattern,
            match=self.match or _SIGNAL_DEFAULT_MATCH[self.signal_type],
            weight=self.weight,
            page_scope=self.page_scope,
            enabled=self.enabled,
            needs_verification=False,
            source_file=ADMIN_SOURCE,
            note=self.note,
        )


def validate(draft: RuleDraft) -> None:
    if not draft.rule_id or len(draft.rule_id) > 128 or " " in draft.rule_id:
        raise ValidationError("rule_id must be a non-empty identifier without spaces")
    if draft.match is not None and draft.match not in MATCH_KINDS:
        raise ValidationError("unknown match kind", match=draft.match)
    if not (0.0 < draft.weight <= 1.0):
        raise ValidationError("weight must be in (0, 1]", weight=draft.weight)
    try:
        _validate_pattern(
            draft.signal_type,
            draft.match or _SIGNAL_DEFAULT_MATCH[draft.signal_type],
            draft.pattern,
            where=draft.rule_id,
        )
    except RuleError as exc:
        raise ValidationError(str(exc)) from exc


def current_version(session: Session, rule_id: str) -> DetectionRule | None:
    return session.execute(
        select(DetectionRule)
        .where(DetectionRule.rule_id == rule_id)
        .order_by(DetectionRule.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def versions_of(session: Session, rule_id: str) -> list[DetectionRule]:
    rows = session.execute(
        select(DetectionRule)
        .where(DetectionRule.rule_id == rule_id)
        .order_by(DetectionRule.version.desc())
    ).scalars()
    rows_list = list(rows)
    if not rows_list:
        raise NotFoundError("rule not found", rule_id=rule_id)
    return rows_list


def list_current(
    session: Session, *, target_type: RuleTargetType | None = None, query: str | None = None
) -> list[DetectionRule]:
    """Latest version of every rule, optionally filtered by target type / substring."""
    latest = (
        select(DetectionRule.rule_id, func.max(DetectionRule.version).label("v"))
        .group_by(DetectionRule.rule_id)
        .subquery()
    )
    q = (
        select(DetectionRule)
        .join(
            latest,
            (DetectionRule.rule_id == latest.c.rule_id) & (DetectionRule.version == latest.c.v),
        )
        .order_by(DetectionRule.target_type, DetectionRule.target_id, DetectionRule.rule_id)
    )
    if target_type is not None:
        q = q.where(DetectionRule.target_type == target_type)
    if query:
        like = f"%{query.strip()}%"
        q = q.where(
            DetectionRule.rule_id.ilike(like)
            | DetectionRule.target_id.ilike(like)
            | DetectionRule.pattern.ilike(like)
        )
    return list(session.execute(q).scalars())


def _row_dict(row: DetectionRule) -> dict[str, Any]:
    return {
        "rule_id": row.rule_id,
        "version": row.version,
        "target_type": row.target_type.value,
        "target_id": row.target_id,
        "signal_type": row.signal_type.value,
        "pattern": row.pattern,
        "weight": row.weight,
        "page_scope": row.page_scope.value,
        "enabled": row.enabled,
        "source_file": row.source_file,
    }


def save_version(
    session: Session, draft: RuleDraft, *, actor: str, ip: str | None, clock: Clock
) -> DetectionRule:
    """Create the rule (version 1) or a new version of an existing rule."""
    validate(draft)
    prev = current_version(session, draft.rule_id)
    version = 1 if prev is None else prev.version + 1
    row = DetectionRule(
        rule_id=draft.rule_id,
        version=version,
        target_type=draft.target_type,
        target_id=draft.target_id,
        signal_type=draft.signal_type,
        pattern=draft.pattern,
        weight=draft.weight,
        page_scope=draft.page_scope,
        enabled=draft.enabled,
        needs_verification=False,
        source_file=ADMIN_SOURCE,
        loaded_at=clock.now(),
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="rule.create" if prev is None else "rule.update",
        object_type="detection_rule",
        object_id=draft.rule_id,
        before=_row_dict(prev) if prev is not None else None,
        after=_row_dict(row),
        ip=ip,
        clock=clock,
    )
    return row


def set_enabled(
    session: Session, rule_id: str, enabled: bool, *, actor: str, ip: str | None, clock: Clock
) -> DetectionRule:
    """Enable or disable the current version. Disabling is the admin "delete"."""
    row = current_version(session, rule_id)
    if row is None:
        raise NotFoundError("rule not found", rule_id=rule_id)
    before = _row_dict(row)
    row.enabled = enabled
    row.loaded_at = clock.now()
    session.flush()
    audit.record(
        session,
        actor=actor,
        action="rule.enable" if enabled else "rule.disable",
        object_type="detection_rule",
        object_id=rule_id,
        before=before,
        after=_row_dict(row),
        ip=ip,
        clock=clock,
    )
    return row


def rule_of(row: DetectionRule) -> Rule:
    return Rule(
        rule_id=row.rule_id,
        version=row.version,
        target_type=row.target_type,
        target_id=row.target_id,
        signal_type=row.signal_type,
        pattern=row.pattern,
        match=_SIGNAL_DEFAULT_MATCH[row.signal_type],
        weight=row.weight,
        page_scope=row.page_scope,
        enabled=row.enabled,
        needs_verification=row.needs_verification,
        source_file=row.source_file,
    )


def overlay(session: Session, base: RuleSet) -> RuleSet:
    """YAML ruleset with database state applied: for every rule_id the highest
    database version replaces the YAML rule (admin edits, enable/disable)."""
    by_id: dict[str, Rule] = {}
    for r in base.rules:
        if r.rule_id not in by_id or r.version > by_id[r.rule_id].version:
            by_id[r.rule_id] = r
    for row in list_current(session):
        current = by_id.get(row.rule_id)
        if current is None or row.version > current.version or row.source_file == ADMIN_SOURCE:
            by_id[row.rule_id] = rule_of(row)
        elif row.version == current.version and row.enabled != current.enabled:
            by_id[row.rule_id] = Rule(**{**current.__dict__, "enabled": row.enabled})
    return RuleSet(rules=tuple(sorted(by_id.values(), key=lambda r: (r.rule_id, r.version))))


# --- gold-set preview -------------------------------------------------------


@dataclass
class Preview:
    gold_hosts: int
    observed_hosts: int
    fired: int
    tp: int = 0
    fp: int = 0
    fn: int = 0
    fired_hosts: list[int] = field(default_factory=list)
    false_positive_hosts: list[int] = field(default_factory=list)
    note: str = ""

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


def _signals_of_gold_hosts(ch: Any, host_ids: list[int]) -> dict[int, PageSignals]:
    """Observed signals per gold host, folded into one `PageSignals` of scope `any`."""
    out: dict[int, PageSignals] = {}
    if not host_ids:
        return out
    for table in _OBS_TABLES:
        rows = ch.query(
            f"SELECT DISTINCT host_id, signal_type, signal_value, page_type FROM {table} "  # noqa: S608 - table from a closed tuple
            "WHERE host_id IN %(hosts)s",
            parameters={"hosts": host_ids},
        ).result_rows
        for host_id, signal_type, value, _page_type in rows:
            s = out.setdefault(int(host_id), PageSignals(page_type="checkout", url=""))
            _fold(s, str(signal_type), str(value))
    return out


def _fold(s: PageSignals, signal_type: str, value: str) -> None:
    st = signal_type
    if st in (SignalType.SCRIPT_SRC.value, SignalType.PLATFORM_PLUGIN.value):
        s.script_srcs.append(value)
    elif st == SignalType.NETWORK_HOST.value:
        s.network_hosts.add(value)
    elif st == SignalType.IFRAME_SRC.value:
        s.iframe_srcs.append(value)
    elif st == SignalType.FORM_ACTION.value:
        s.form_actions.append(value)
    elif st == SignalType.JS_GLOBAL.value:
        s.js_globals.add(value)
    elif st == SignalType.HTML_PATTERN.value:
        s.html += value + "\n"
    elif st == SignalType.HEADER.value:
        name, _, rest = value.partition(":")
        s.headers[name.strip().lower()] = rest.strip()
    elif st == SignalType.COOKIE.value:
        s.cookie_names.add(value)
    elif st == SignalType.FAVICON.value:
        s.favicon_sha256 = value
    elif st == SignalType.CHECKOUT_LABEL.value:
        s.checkout_labels.append(value)


def preview(session: Session, draft: RuleDraft, *, ch: Any) -> Preview:
    """Fire the draft over observed signals of gold hosts; score against gold labels.

    Observed signals are what previous scans recorded, so a pattern that no
    existing rule ever matched can only be previewed against those values; the
    note says so. Without ClickHouse the preview reports the gold size only.
    """
    validate(draft)
    gold_type = _GOLD_TYPE[draft.target_type]
    present: dict[int, set[str]] = defaultdict(set)
    hosts: set[int] = set()
    if gold_type is not None:
        for label in session.execute(
            select(GoldLabel).where(GoldLabel.entity_type == gold_type)
        ).scalars():
            hosts.add(label.host_id)
            if label.present:
                present[label.host_id].add(label.entity_id)
    result = Preview(gold_hosts=len(hosts), observed_hosts=0, fired=0)
    if gold_type is None:
        result.note = "tech rules have no gold labels; preview reports firings only"
    if ch is None:
        result.note = (result.note + "; " if result.note else "") + "ClickHouse unavailable"
        return result
    signals = _signals_of_gold_hosts(ch, sorted(hosts))
    result.observed_hosts = len(signals)
    rule = draft.as_rule(version=0)
    rule = Rule(**{**rule.__dict__, "enabled": True, "page_scope": PageScope.ANY})
    rs = RuleSet(rules=(rule,))
    fired: set[int] = set()
    for host_id, s in signals.items():
        if match_page(rs, s):
            fired.add(host_id)
    result.fired = len(fired)
    result.fired_hosts = sorted(fired)
    if gold_type is None:
        return result
    for host_id in fired:
        if draft.target_id in present.get(host_id, set()):
            result.tp += 1
        else:
            result.fp += 1
            result.false_positive_hosts.append(host_id)
    for host_id, truth in present.items():
        if draft.target_id in truth and host_id not in fired:
            result.fn += 1
    result.false_positive_hosts.sort()
    if not result.note:
        result.note = "scored over signals recorded by earlier scans of gold hosts"
    return result
