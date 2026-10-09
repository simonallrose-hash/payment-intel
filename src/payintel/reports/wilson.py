"""Wilson score interval for a binomial share (FR-RP-04)."""

from __future__ import annotations

import math

Z_95 = 1.959963984540054


def wilson(successes: int, total: int, *, z: float = Z_95) -> tuple[float, float]:
    """95 % interval for `successes / total`; (0, 0) when total is 0."""
    if total <= 0:
        return 0.0, 0.0
    p = min(max(successes, 0), total) / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)
