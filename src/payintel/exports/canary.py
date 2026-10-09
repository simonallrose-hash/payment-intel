"""Canary records (FR-EX-05, FR-AB-04): 3–10 synthetic stores per export.

Domains are `c-<12 hex>.<canary zone>` under company control, so any DNS or
HTTP access to one of them can be attributed to the export that contained
it. The row values are deterministic in the export id so a re-run yields the
same canaries; the synthetic providers and methods are plausible but the
`canary` table is the only place that marks them as fake.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from payintel.core.models.exports import Canary

_PROVIDERS = ("stripe", "adyen", "paypal", "mollie", "klarna", "checkout_com", "worldpay")
_METHODS = ("visa", "mastercard", "paypal", "apple_pay", "klarna", "ideal", "google_pay")
_PLATFORMS = ("woocommerce", "shopify", "magento2", "shopware6", "prestashop")


def canary_count(export_id: uuid.UUID, *, low: int, high: int) -> int:
    digest = hashlib.sha256(b"count" + export_id.bytes).digest()
    return low + digest[0] % (high - low + 1)


def _pick(seed: bytes, options: tuple[str, ...], n: int) -> list[str]:
    out: list[str] = []
    for i in range(n):
        d = hashlib.sha256(seed + bytes([i])).digest()
        choice = options[d[0] % len(options)]
        if choice not in out:
            out.append(choice)
    return out


def canary_rows(
    session: Session,
    *,
    org_id: uuid.UUID,
    export_id: uuid.UUID,
    zone: str,
    countries: list[str],
    platforms: list[str],
    now: datetime,
    low: int,
    high: int,
) -> list[dict[str, Any]]:
    """Insert `Canary` rows and return snapshot-shaped records for the file."""
    rows: list[dict[str, Any]] = []
    for i in range(canary_count(export_id, low=low, high=high)):
        seed = hashlib.sha256(export_id.bytes + bytes([i])).digest()
        domain = f"c-{seed[:6].hex()}.{zone}"
        session.add(Canary(domain=domain, org_id=org_id, export_job_id=export_id, created_at=now))
        providers = _pick(seed + b"p", _PROVIDERS, 1 + seed[7] % 2)
        methods = _pick(seed + b"m", _METHODS, 2 + seed[8] % 3)
        country = countries[seed[9] % len(countries)] if countries else "DE"
        platform = platforms[seed[10] % len(platforms)] if platforms else _PLATFORMS[seed[10] % 5]
        as_of = now - timedelta(days=seed[11] % 20)
        rows.append(
            {
                "domain": domain,
                "as_of": as_of,
                "platform_id": platform,
                "platform_confidence": "high",
                "country": country,
                "country_confidence": "high",
                "currency": "EUR",
                "vertical_id": None,
                "checkout_status": "reached_payment_step",
                "coverage": "payment_step",
                "acquirer_hidden": False,
                "provider_ids": providers,
                "providers": [f"{p}:gateway:high" for p in providers],
                "providers_active_on_checkout": providers,
                "method_ids": methods,
                "methods": [f"{m}:card:high" for m in methods],
                "checkout_psp_hosts": [f"{p.replace('_', '')}.com" for p in providers],
                "traffic_rank": None,
                "first_seen": (as_of - timedelta(days=90)).date(),
                "last_seen": as_of.date(),
                "last_checkout_scan_at": as_of,
            }
        )
    session.flush()
    return rows
