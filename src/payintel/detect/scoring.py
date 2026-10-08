"""Confidence scoring (FR-DT-04, FR-DT-05).

Per target (provider / method / platform) the independent signals are the
distinct `signal_type`s that fired. Rules:

- `high`: a network-level signal (`network_host`, `iframe_src`, `js_global`)
  seen on the checkout, or ≥2 independent signal types;
- `medium`: one checkout signal of another type;
- `low`: signals only from homepage / product / cart pages.

For platforms the same ladder applies, but the light scan may award `high`
on two independent signals (platform markers are page-level by nature).
`active_on_checkout` is true only when at least one signal came from a
checkout-depth page (FR-DT-05). The numeric score is the capped weight sum.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from payintel.core.models.base import ConfidenceLevel, RuleTargetType, SignalType
from payintel.detect.engine import Finding

NETWORK_LEVEL = {SignalType.NETWORK_HOST, SignalType.IFRAME_SRC, SignalType.JS_GLOBAL}
CHECKOUT_PAGES = {"checkout", "payment_step"}


@dataclass
class TargetScore:
    target_type: RuleTargetType
    target_id: str
    score: float
    confidence: ConfidenceLevel
    active_on_checkout: bool
    signal_types: set[SignalType] = field(default_factory=set)
    findings: list[Finding] = field(default_factory=list)


def _level(findings: list[Finding], *, is_platform: bool) -> ConfidenceLevel:
    on_checkout = [f for f in findings if f.page_type in CHECKOUT_PAGES]
    types = {f.rule.signal_type for f in findings}
    if any(f.rule.signal_type in NETWORK_LEVEL for f in on_checkout):
        return ConfidenceLevel.HIGH
    if len(types) >= 2 and (on_checkout or is_platform):
        return ConfidenceLevel.HIGH
    if on_checkout:
        return ConfidenceLevel.MEDIUM
    if is_platform and sum(f.rule.weight for f in findings) >= 0.7:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def aggregate(findings: list[Finding]) -> list[TargetScore]:
    groups: dict[tuple[RuleTargetType, str], list[Finding]] = {}
    for f in findings:
        groups.setdefault((f.rule.target_type, f.rule.target_id), []).append(f)
    out: list[TargetScore] = []
    for (tt, tid), fs in sorted(groups.items(), key=lambda kv: (kv[0][0].value, kv[0][1])):
        # one weight per signal type (the best), so five html_pattern hits do not stack
        best: dict[SignalType, float] = {}
        for f in fs:
            best[f.rule.signal_type] = max(best.get(f.rule.signal_type, 0.0), f.rule.weight)
        score = min(1.0, round(sum(best.values()), 3))
        out.append(
            TargetScore(
                target_type=tt,
                target_id=tid,
                score=score,
                confidence=_level(fs, is_platform=tt == RuleTargetType.PLATFORM),
                active_on_checkout=any(f.page_type in CHECKOUT_PAGES for f in fs),
                signal_types=set(best),
                findings=fs,
            )
        )
    return out


def best_platform(scores: list[TargetScore]) -> TargetScore | None:
    platforms = [s for s in scores if s.target_type == RuleTargetType.PLATFORM]
    if not platforms:
        return None
    return max(platforms, key=lambda s: (s.score, len(s.signal_types), s.target_id))
