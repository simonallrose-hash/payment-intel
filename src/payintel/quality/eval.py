"""Precision / recall / F1 on the gold set (FR-QA-02, AC-03, NFR-Q-01..05).

Truth: `gold_label` rows. For a labelled host the set of entities with
`present=true` is taken as the complete truth for that entity type, so a
predicted entity that is not labelled present counts as a false positive.

Predictions: the materialised current state (`store_provider`,
`store_payment_method`, `store_profile`), optionally restricted to a minimum
confidence. Per-rule metrics come from ClickHouse `obs_provider` /
`obs_payment_method` (which rule fired for which host) and are skipped with a
note when ClickHouse is unavailable (NFR-R-06 spirit: Postgres alone suffices).

The PSP precision gate (`--min-psp-precision`, default 0.95) makes `make eval`
fail so that a rule release is blocked (FR-QA-02).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any

from clickhouse_connect.driver.client import Client
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.models.base import ConfidenceLevel, GoldEntityType
from payintel.core.models.quality import GoldLabel
from payintel.core.models.store import StorePaymentMethod, StoreProfile, StoreProvider

_CONF_ORDER = {ConfidenceLevel.LOW: 0, ConfidenceLevel.MEDIUM: 1, ConfidenceLevel.HIGH: 2}
_PER_RULE_SOURCES: frozenset[tuple[str, str]] = frozenset(
    {("obs_provider", "provider_id"), ("obs_payment_method", "method_id")}
)


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        denom = self.tp + self.fp
        return self.tp / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.tp + self.fn
        return self.tp / denom if denom else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
        }


@dataclass
class EntityTypeReport:
    labeled_hosts: int
    overall: Counts
    per_entity: dict[str, Counts]
    per_rule: dict[str, Counts] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "labeled_hosts": self.labeled_hosts,
            "overall": self.overall.as_dict(),
            "per_entity": {k: v.as_dict() for k, v in sorted(self.per_entity.items())},
            "per_rule": {k: v.as_dict() for k, v in sorted(self.per_rule.items())},
        }


@dataclass
class EvalReport:
    gold_hosts: int
    min_confidence: str
    provider: EntityTypeReport
    payment_method: EntityTypeReport
    platform: EntityTypeReport
    country: EntityTypeReport
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "gold_hosts": self.gold_hosts,
            "min_confidence": self.min_confidence,
            "provider": self.provider.as_dict(),
            "payment_method": self.payment_method.as_dict(),
            "platform": self.platform.as_dict(),
            "country": self.country.as_dict(),
            "notes": list(self.notes),
        }

    def psp_precision(self) -> float | None:
        return self.provider.overall.precision

    def passes_gate(self, min_psp_precision: float) -> bool:
        """True when PSP precision is above the gate or there is nothing to measure."""
        p = self.psp_precision()
        return p is None or p >= min_psp_precision


@dataclass
class GateResult:
    passed: bool
    reason: str


def _truth(
    session: Session, entity_type: GoldEntityType
) -> tuple[dict[int, set[str]], dict[int, set[str]]]:
    """Returns (present_by_host, absent_by_host) for labelled hosts of `entity_type`."""
    present: dict[int, set[str]] = defaultdict(set)
    absent: dict[int, set[str]] = defaultdict(set)
    for label in session.execute(
        select(GoldLabel).where(GoldLabel.entity_type == entity_type)
    ).scalars():
        (present if label.present else absent)[label.host_id].add(label.entity_id)
        # make sure the host is registered even if it only has absent labels
        present.setdefault(label.host_id, set())
    return present, absent


def _score_sets(
    present: dict[int, set[str]], predicted: dict[int, set[str]]
) -> tuple[Counts, dict[str, Counts]]:
    overall = Counts()
    per_entity: dict[str, Counts] = defaultdict(Counts)
    for host_id, truth in present.items():
        pred = predicted.get(host_id, set())
        for e in pred & truth:
            overall.tp += 1
            per_entity[e].tp += 1
        for e in pred - truth:
            overall.fp += 1
            per_entity[e].fp += 1
        for e in truth - pred:
            overall.fn += 1
            per_entity[e].fn += 1
    return overall, dict(per_entity)


def _predicted_multi(
    session: Session, entity_type: GoldEntityType, hosts: set[int], min_conf: ConfidenceLevel
) -> dict[int, set[str]]:
    predicted: dict[int, set[str]] = defaultdict(set)
    if not hosts:
        return predicted
    threshold = _CONF_ORDER[min_conf]
    if entity_type == GoldEntityType.PROVIDER:
        for sp in session.execute(
            select(StoreProvider).where(StoreProvider.host_id.in_(hosts))
        ).scalars():
            if _CONF_ORDER[sp.confidence] >= threshold:
                predicted[sp.host_id].add(sp.provider_id)
    else:
        for spm in session.execute(
            select(StorePaymentMethod).where(StorePaymentMethod.host_id.in_(hosts))
        ).scalars():
            if _CONF_ORDER[spm.confidence] >= threshold:
                predicted[spm.host_id].add(spm.method_id)
    return predicted


def _predicted_single(
    session: Session, entity_type: GoldEntityType, hosts: set[int], min_conf: ConfidenceLevel
) -> dict[int, set[str]]:
    predicted: dict[int, set[str]] = defaultdict(set)
    if not hosts:
        return predicted
    for profile in session.execute(
        select(StoreProfile).where(StoreProfile.host_id.in_(hosts))
    ).scalars():
        if entity_type == GoldEntityType.PLATFORM and profile.platform_id:
            conf = profile.platform_confidence or ConfidenceLevel.LOW
            if _CONF_ORDER[conf] >= _CONF_ORDER[min_conf]:
                predicted[profile.host_id].add(profile.platform_id)
        if entity_type == GoldEntityType.COUNTRY and profile.country:
            conf = profile.country_confidence or ConfidenceLevel.LOW
            if _CONF_ORDER[conf] >= _CONF_ORDER[min_conf]:
                predicted[profile.host_id].add(profile.country)
    return predicted


def _per_rule(
    ch: Client, table: str, id_column: str, present: dict[int, set[str]]
) -> dict[str, Counts]:
    """Per-rule precision from ClickHouse: a firing on (host, entity) is TP iff gold says present.

    Per-rule recall counts, for each rule's target entity, the gold positives
    the rule did not fire on.
    """
    if not present:
        return {}
    if (table, id_column) not in _PER_RULE_SOURCES:
        raise ValueError(f"unexpected per-rule source {table}.{id_column}")
    host_ids = list(present.keys())
    rows = ch.query(
        f"SELECT DISTINCT rule_id, host_id, {id_column} FROM {table} WHERE host_id IN %(hosts)s",  # noqa: S608 - identifiers from the closed set above
        parameters={"hosts": host_ids},
    ).result_rows
    fired: dict[str, set[tuple[int, str]]] = defaultdict(set)
    rule_targets: dict[str, set[str]] = defaultdict(set)
    for rule_id, host_id, entity in rows:
        fired[str(rule_id)].add((int(host_id), str(entity)))
        rule_targets[str(rule_id)].add(str(entity))
    out: dict[str, Counts] = {}
    for rule_id, pairs in fired.items():
        c = Counts()
        for host_id, entity in pairs:
            if entity in present.get(host_id, set()):
                c.tp += 1
            else:
                c.fp += 1
        for entity in rule_targets[rule_id]:
            for host_id, truth in present.items():
                if entity in truth and (host_id, entity) not in pairs:
                    c.fn += 1
        out[rule_id] = c
    return out


def evaluate(
    session: Session,
    *,
    ch: Client | None = None,
    min_confidence: ConfidenceLevel = ConfidenceLevel.LOW,
) -> EvalReport:
    notes: list[str] = []
    reports: dict[GoldEntityType, EntityTypeReport] = {}
    all_hosts: set[int] = set()
    for entity_type in GoldEntityType:
        present, _absent = _truth(session, entity_type)
        hosts = set(present.keys())
        all_hosts |= hosts
        if entity_type in (GoldEntityType.PROVIDER, GoldEntityType.PAYMENT_METHOD):
            predicted = _predicted_multi(session, entity_type, hosts, min_confidence)
        else:
            predicted = _predicted_single(session, entity_type, hosts, min_confidence)
        overall, per_entity = _score_sets(present, predicted)
        per_rule: dict[str, Counts] = {}
        if ch is not None and entity_type == GoldEntityType.PROVIDER:
            per_rule = _per_rule(ch, "obs_provider", "provider_id", present)
        elif ch is not None and entity_type == GoldEntityType.PAYMENT_METHOD:
            per_rule = _per_rule(ch, "obs_payment_method", "method_id", present)
        reports[entity_type] = EntityTypeReport(
            labeled_hosts=len(hosts), overall=overall, per_entity=per_entity, per_rule=per_rule
        )
    if ch is None:
        notes.append("ClickHouse client not provided: per-rule metrics skipped")
    if not all_hosts:
        notes.append(
            "gold set is empty: nothing to evaluate (import labels with `payintel gold import`)"
        )
    return EvalReport(
        gold_hosts=len(all_hosts),
        min_confidence=min_confidence.value,
        provider=reports[GoldEntityType.PROVIDER],
        payment_method=reports[GoldEntityType.PAYMENT_METHOD],
        platform=reports[GoldEntityType.PLATFORM],
        country=reports[GoldEntityType.COUNTRY],
        notes=notes,
    )


def gate(report: EvalReport, *, min_psp_precision: float) -> GateResult:
    p = report.psp_precision()
    if p is None:
        return GateResult(True, "no PSP predictions on labelled hosts; gate not applicable")
    if p >= min_psp_precision:
        return GateResult(True, f"PSP precision {p:.3f} >= {min_psp_precision:.2f}")
    return GateResult(
        False, f"PSP precision {p:.3f} < {min_psp_precision:.2f} (FR-QA-02): release blocked"
    )


def summary_lines(report: EvalReport) -> list[str]:
    def fmt(c: Counts) -> str:
        def f(v: float | None) -> str:
            return "n/a" if v is None else f"{v:.3f}"

        return f"P={f(c.precision)} R={f(c.recall)} F1={f(c.f1)} (tp={c.tp} fp={c.fp} fn={c.fn})"

    return [
        f"gold hosts: {report.gold_hosts} (min confidence {report.min_confidence})",
        f"provider        {fmt(report.provider.overall)}",
        f"payment_method  {fmt(report.payment_method.overall)}",
        f"platform        {fmt(report.platform.overall)}",
        f"country         {fmt(report.country.overall)}",
        *[f"note: {n}" for n in report.notes],
    ]


__all__ = [
    "Counts",
    "EntityTypeReport",
    "EvalReport",
    "GateResult",
    "asdict",
    "evaluate",
    "gate",
    "summary_lines",
]
