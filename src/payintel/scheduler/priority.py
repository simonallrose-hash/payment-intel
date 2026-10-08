"""Scan priority (FR-SC-02): product of weighted factors.

priority = (1 + w_e·ecommerce) · (1 + w_r·rank_score) · (1 + w_c·has_checkout)
         · (1 + w_w·on_watchlist) · (1 + w_s·staleness)

rank_score = 1 − log10(rank)/6 clipped to [0, 1] (Tranco #1 → 1.0, #1M → 0);
staleness = days since last successful scan ÷ planned interval, capped at 3
(never scanned → 3). All weights come from `ScanSettings`, so the formula is
tunable without code changes (NFR-M-02). Higher is scanned first.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from payintel.core.settings import ScanSettings

STALENESS_CAP = 3.0


@dataclass(frozen=True)
class PriorityInputs:
    is_ecommerce: bool
    traffic_rank: int | None
    has_checkout: bool
    on_watchlist: bool
    days_since_success: float | None
    interval_days: int


def rank_score(rank: int | None) -> float:
    if rank is None or rank < 1:
        return 0.0
    return max(0.0, min(1.0, 1.0 - math.log10(rank) / 6.0))


def staleness(days_since_success: float | None, interval_days: int) -> float:
    if days_since_success is None:
        return STALENESS_CAP
    return max(0.0, min(STALENESS_CAP, days_since_success / max(interval_days, 1)))


def compute_priority(inp: PriorityInputs, s: ScanSettings) -> float:
    value = (
        (1.0 + s.priority_weight_ecommerce * float(inp.is_ecommerce))
        * (1.0 + s.priority_weight_traffic_rank * rank_score(inp.traffic_rank))
        * (1.0 + s.priority_weight_has_checkout * float(inp.has_checkout))
        * (1.0 + s.priority_weight_watchlist * float(inp.on_watchlist))
        * (1.0 + s.priority_weight_staleness * staleness(inp.days_since_success, inp.interval_days))
    )
    return round(value, 4)


MANUAL_PRIORITY = 1_000_000.0  # FR-SC-07: always ahead of computed priorities
