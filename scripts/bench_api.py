#!/usr/bin/env python
"""NFR-P-03/P-04/P-05 load run against a running API (AC-12).

    make bench-data            # ~1M synthetic stores into $PAYINTEL_POSTGRES__DSN
    payintel api serve --port 8900 --workers 2
    uv run python scripts/bench_api.py --base-url http://127.0.0.1:8900 \\
        --api-key "$KEY" --duration 60 --out bench-api.json

Exit status 1 when any scenario misses its threshold. The JSON report carries
per-scenario p50/p95/p99 and the raw latencies for `docs/capacity.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from payintel.bench.api_load import default_scenarios, run_all, sample_domains


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--duration", type=float, default=60.0, help="seconds per scenario")
    parser.add_argument("--lookup-rps", type=float, default=20.0)
    parser.add_argument("--search-rps", type=float, default=1.0)
    parser.add_argument("--stats-rps", type=float, default=1.0)
    parser.add_argument("--country", action="append", default=[], help="filter values to mix in")
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--provider", action="append", default=[])
    parser.add_argument("--domains-file", type=Path, help="one domain per line (else sampled)")
    parser.add_argument("--out", type=Path, help="JSON report path")
    args = parser.parse_args()

    if args.domains_file:
        domains = [d.strip() for d in args.domains_file.read_text().splitlines() if d.strip()]
    else:
        domains = asyncio.run(sample_domains(args.base_url, args.api_key))
    scenarios = default_scenarios(
        domains,
        countries=args.country or ["DE", "FR", "PL"],
        platforms=args.platform or ["shopify", "woocommerce"],
        providers=args.provider or ["stripe", "adyen"],
        duration_s=args.duration,
        lookup_rps=args.lookup_rps,
        search_rps=args.search_rps,
        stats_rps=args.stats_rps,
    )
    results = asyncio.run(run_all(args.base_url, args.api_key, scenarios))
    rows = [r.as_dict() for r in results]
    for r in rows:
        mark = "PASS" if r["passed"] else "FAIL"
        print(
            f"{mark} {r['requirement']} {r['name']}: n={r['requests']} err={r['errors']} "
            f"rps={r['rps_achieved']} p50={r['p50_ms']}ms p95={r['p95_ms']}ms "
            f"p99={r['p99_ms']}ms max={r['max_ms']}ms "
            f"(p95≤{r['threshold_p95_ms']}, p99≤{r['threshold_p99_ms']})"
        )
    if args.out:
        args.out.write_text(
            json.dumps(
                {
                    "domains_sampled": len(domains),
                    "scenarios": rows,
                    "latencies_ms": {
                        r.name: [round(x, 2) for x in r.latencies_ms] for r in results
                    },
                },
                indent=1,
            )
        )
    return 0 if all(r["passed"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
