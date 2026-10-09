"""Synthetic stand for the NFR-P load runs: `make bench-data` (not in CI).

Writes N synthetic stores (default 1 000 000) into the configured Postgres
(PAYINTEL_POSTGRES__DSN — use a dedicated database, never production) and
prints the API key of the bench client. Requires migrations and `payintel seed`.

Usage: python scripts/synth_dataset.py --stores 1000000 [--seed 42] [--reset]
"""

from __future__ import annotations

import argparse
import sys
import time

from payintel.bench.synthetic import SynthSpec, generate
from payintel.core.clock import SYSTEM_CLOCK
from payintel.core.db import get_engine
from payintel.core.settings import get_settings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stores", type=int, default=1_000_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--reset", action="store_true", help="truncate the store tables first")
    args = ap.parse_args()
    settings = get_settings()
    pepper = settings.secrets.api_key_pepper.get_secret_value()
    if not pepper:
        print("PAYINTEL_SECRETS__API_KEY_PEPPER is not set", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    res = generate(
        get_engine(),
        SynthSpec(stores=args.stores, seed=args.seed),
        clock=SYSTEM_CLOCK,
        pepper=pepper,
        key_prefix=settings.api.api_key_prefix,
        reset=args.reset,
        progress=lambda m: print(m, file=sys.stderr),
    )
    print(
        f"stores={res.stores} providers={res.providers} methods={res.methods} "
        f"checkout_hosts={res.checkout_hosts} events={res.events} "
        f"in {time.monotonic() - t0:.0f}s; org={res.org_id}"
    )
    print(f"api_key={res.api_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
