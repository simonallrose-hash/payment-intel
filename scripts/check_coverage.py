"""Enforce per-package coverage floors from NFR-M-01.

Usage: python scripts/check_coverage.py coverage.json

`detect`, `entitlements`, `crawl/checkout/guardrails` and `exports` must reach
85% line coverage, everything else 70%. Packages that do not exist yet (later
stages) are skipped; the global floor is enforced by `fail_under` in
pyproject.toml.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

STRICT_PACKAGES: dict[str, float] = {
    "payintel/detect/": 85.0,
    "payintel/entitlements/": 85.0,
    "payintel/crawl/checkout/guardrails/": 85.0,
    "payintel/exports/": 85.0,
}
DEFAULT_FLOOR = 70.0


def package_of(path: str) -> str:
    """Map a file path to the first-level package under payintel (e.g. `payintel/core/`)."""
    normalized = path.replace("\\", "/")
    idx = normalized.find("payintel/")
    if idx < 0:
        return normalized
    rest = normalized[idx + len("payintel/") :]
    parts = rest.split("/")
    if len(parts) == 1:
        return "payintel/"
    return "payintel/" + parts[0] + "/"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    data = json.loads(Path(argv[1]).read_text())
    totals: dict[str, list[int]] = {}
    for file_path, info in data["files"].items():
        summary = info["summary"]
        norm = file_path.replace("\\", "/")
        keys = [k for k in STRICT_PACKAGES if k in norm] or [package_of(norm)]
        for key in keys:
            covered, statements = totals.setdefault(key, [0, 0])
            totals[key] = [
                covered + summary["covered_lines"],
                statements + summary["num_statements"],
            ]
    failed = False
    for key, (covered, statements) in sorted(totals.items()):
        if statements == 0:
            continue
        pct = 100.0 * covered / statements
        floor = STRICT_PACKAGES.get(key, DEFAULT_FLOOR)
        status = "ok " if pct >= floor else "LOW"
        print(f"{status} {key:45s} {pct:6.1f}% (floor {floor:.0f}%)")
        failed |= pct < floor
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
