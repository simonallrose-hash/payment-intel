"""Synthetic C1 dataset for the load runs (NFR-P-03…P-06, AC-12; `scripts/synth_dataset.py`).

Fills `domain`, `host`, `domain_source`, `store_profile`, `store_provider`,
`store_payment_method`, `store_checkout_host`, `scan_run` and `change_event`
with `COPY` for N stores (default 1 000 000) drawn from skewed but plausible
distributions (countries, platforms, Zipf-like provider and method
popularity), plus one active client organisation with an unrestricted
`c1_full` entitlement and an API key. Everything is derived from a seed, so a
run is reproducible; nothing here is a real shop (domains are `shopNNNNNNN.<tld>`).

The data is for a *stand*, never for a production database: `generate()`
refuses to run when any store_profile row exists unless `reset=True`.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from payintel.api.auth import keys as keys_mod
from payintel.core.clock import Clock
from payintel.core.models.base import FieldProfile, OrgStatus, Product
from payintel.core.models.orgs import Contract, Entitlement, Organization
from payintel.core.models.store import StoreProfile
from payintel.core.reference_loader import ReferenceData, load_reference
from payintel.entitlements.model import SCOPES

COUNTRIES: tuple[tuple[str, float], ...] = (
    ("DE", 0.30), ("GB", 0.10), ("FR", 0.12), ("NL", 0.08), ("PL", 0.08), ("ES", 0.07),
    ("IT", 0.07), ("AT", 0.04), ("CH", 0.03), ("BE", 0.03), ("SE", 0.02), ("DK", 0.02),
    ("CZ", 0.02), ("PT", 0.01), ("IE", 0.01),
)  # fmt: skip
PLATFORMS: tuple[tuple[str, float], ...] = (
    ("woocommerce", 0.35), ("shopify", 0.20), ("magento2", 0.07), ("prestashop", 0.07),
    ("shopware6", 0.06), ("wix", 0.05), ("custom", 0.04), ("squarespace", 0.03),
    ("bigcommerce", 0.03), ("opencart", 0.03), ("shoptet", 0.02), ("jtl", 0.02),
    ("shopware5", 0.01), ("magento1", 0.01), ("oxid", 0.01),
)  # fmt: skip
TLD_OF: dict[str, tuple[str, ...]] = {
    "DE": ("de", "com", "shop"), "GB": ("co.uk", "com"), "FR": ("fr", "com"), "NL": ("nl", "com"),
    "PL": ("pl", "com"), "ES": ("es", "com"), "IT": ("it", "com"), "AT": ("at", "com"),
    "CH": ("ch", "com"), "BE": ("be", "com"), "SE": ("se", "com"), "DK": ("dk", "com"),
    "CZ": ("cz", "com"), "PT": ("pt", "com"), "IE": ("ie", "com"),
}  # fmt: skip
SOURCES = ("tranco", "commoncrawl", "ct", "crawl_link")
VERTICALS = (
    "fashion",
    "electronics",
    "home_garden",
    "beauty",
    "sports_outdoor",
    "food_beverage",
    "toys_kids",
    "books_media",
)
EVENT_TYPES = ("provider_added", "provider_removed", "method_added", "method_removed")
BATCH = 50_000


@dataclass(frozen=True)
class SynthSpec:
    stores: int = 1_000_000
    seed: int = 42
    events_share: float = 0.3  # stores with 1–3 change events in the last 90 days
    rank_share: float = 0.4  # stores with a Tranco rank


@dataclass
class SynthResult:
    stores: int
    providers: int
    methods: int
    checkout_hosts: int
    events: int
    org_id: uuid.UUID
    api_key: str


def _weighted(rng: random.Random, table: tuple[tuple[str, float], ...]) -> str:
    return rng.choices([k for k, _ in table], weights=[w for _, w in table], k=1)[0]


def _zipf_weights(n: int, s: float = 0.9) -> list[float]:
    return [1.0 / (i + 1) ** s for i in range(n)]


class _Rows:
    """Row generators for one batch of stores, sharing one RNG stream."""

    def __init__(self, ref: ReferenceData, spec: SynthSpec, now: datetime) -> None:
        self.rng = random.Random(spec.seed)  # noqa: S311 - synthetic data, not security
        self.spec = spec
        self.now = now
        self.today = now.date()
        self.providers = [
            (p["id"], p["role"]) for p in ref.providers if p["status"] != "deprecated"
        ]
        self.prov_weights = _zipf_weights(len(self.providers))
        self.methods = [(m["id"], m.get("default_provider_id")) for m in ref.payment_methods]
        self.method_weights = _zipf_weights(len(self.methods), 0.7)
        self.psp_hosts = {
            "stripe": "stripe.com",
            "adyen": "adyen.com",
            "paypal": "paypal.com",
            "klarna": "klarna.com",
            "mollie": "mollie.com",
            "braintree": "braintreegateway.com",
        }
        self.next_provider_id = 1
        self.next_method_id = 1
        self.next_host_row_id = 1
        self.next_event_id = 1

    def store(self, n: int) -> dict[str, Any]:
        rng = self.rng
        country = _weighted(rng, COUNTRIES)
        platform = _weighted(rng, PLATFORMS)
        tld = rng.choice(TLD_OF[country])
        etld1 = f"shop{n:07d}.{tld}"
        rank = rng.randint(1, 5_000_000) if rng.random() < self.spec.rank_share else None
        created = self.now - timedelta(days=rng.randint(30, 400))
        light_at = self.now - timedelta(hours=rng.randint(1, 24 * 7))
        checkout_at = self.now - timedelta(hours=rng.randint(1, 24 * 30))
        k = rng.choices((1, 2, 3), weights=(0.55, 0.35, 0.10), k=1)[0]
        picked: list[tuple[str, str]] = []
        while len(picked) < k:
            cand = rng.choices(self.providers, weights=self.prov_weights, k=1)[0]
            if cand not in picked:
                picked.append(cand)
        m = rng.randint(3, 6)
        methods: list[tuple[str, str | None]] = []
        while len(methods) < m:
            cand_m = rng.choices(self.methods, weights=self.method_weights, k=1)[0]
            if cand_m not in methods:
                methods.append(cand_m)
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"payintel-bench/{self.spec.seed}/{n}")
        return {
            "n": n,
            "etld1": etld1,
            "tld": tld,
            "country": country,
            "platform": platform,
            "rank": rank,
            "created": created,
            "light_at": light_at,
            "checkout_at": checkout_at,
            "providers": picked,
            "methods": methods,
            "run_id": run_id,
            "source": rng.choice(SOURCES),
            "vertical": rng.choice(VERTICALS),
            "events": (rng.randint(1, 3) if rng.random() < self.spec.events_share else 0),
        }

    # --- COPY rows per table -------------------------------------------------------
    def domain(self, s: dict[str, Any]) -> tuple[Any, ...]:
        return (s["n"], s["etld1"], s["tld"], "ecommerce", False, s["created"], None,
                s["created"], s["rank"], 0.9)  # fmt: skip

    def host(self, s: dict[str, Any]) -> tuple[Any, ...]:
        return (s["n"], s["n"], s["etld1"], True, None, None, s["country"], s["created"],
                None, None, None, "ok", s["light_at"])  # fmt: skip

    def source(self, s: dict[str, Any]) -> tuple[Any, ...]:
        return (s["n"], s["n"], s["source"], s["created"], s["light_at"], f"bench-{s['source']}")

    def profile(self, s: dict[str, Any]) -> tuple[Any, ...]:
        return (s["n"], s["platform"], "high", None, s["country"], "high", "EUR", s["vertical"],
                "medium", "reached_payment_step", "payment_step", s["country"], False,
                s["light_at"], s["checkout_at"], s["rank"], s["checkout_at"])  # fmt: skip

    def provider_rows(self, s: dict[str, Any]) -> Iterator[tuple[Any, ...]]:
        first = self.today - timedelta(days=self.rng.randint(10, 300))
        for pid, role in s["providers"]:
            rid = self.next_provider_id
            self.next_provider_id += 1
            yield (rid, s["n"], pid, role, "high", 0.9, True, first, self.today - timedelta(days=1),
                   3, 0, False)  # fmt: skip

    def method_rows(self, s: dict[str, Any]) -> Iterator[tuple[Any, ...]]:
        present = {pid for pid, _ in s["providers"]}
        for mid, dp in s["methods"]:
            rid = self.next_method_id
            self.next_method_id += 1
            yield (
                rid,
                s["n"],
                mid,
                dp if dp in present else None,
                "medium",
                0.7,
                self.today - timedelta(days=40),
                self.today - timedelta(days=1),
                3,
                0,
                False,
            )

    def host_rows(self, s: dict[str, Any]) -> Iterator[tuple[Any, ...]]:
        for pid, _ in s["providers"]:
            h = self.psp_hosts.get(pid)
            if h is None:
                continue
            rid = self.next_host_row_id
            self.next_host_row_id += 1
            yield (rid, s["n"], h, "psp", pid, 4, self.today - timedelta(days=40),
                   self.today - timedelta(days=1), 3, 0)  # fmt: skip

    def scan_run(self, s: dict[str, Any]) -> tuple[Any, ...]:
        return (s["run_id"], s["n"], "checkout", s["checkout_at"], s["checkout_at"],
                "reached_payment_step", "payment_step", None, None, s["country"], False,
                "bench", "bench", None, None)  # fmt: skip

    def event_rows(self, s: dict[str, Any]) -> Iterator[tuple[Any, ...]]:
        for _ in range(s["events"]):
            eid = self.next_event_id
            self.next_event_id += 1
            kind = self.rng.choice(EVENT_TYPES)
            entity_type = "provider" if kind.startswith("provider") else "payment_method"
            entity = s["providers"][0][0] if entity_type == "provider" else s["methods"][0][0]
            at = self.now - timedelta(hours=self.rng.randint(1, 24 * 90))
            old = entity if kind.endswith("removed") else None
            new = None if kind.endswith("removed") else entity
            yield (eid, s["n"], kind, entity_type, entity, old, new, at, s["run_id"], False)


COPY_SQL = {
    "domain": "COPY domain (id, etld1, tld, status, optout, created_at, status_reason, "
    "status_changed_at, traffic_rank, ecommerce_confidence) FROM STDIN",
    "host": "COPY host (id, domain_id, hostname, is_primary, last_resolved_ips, asn, "
    "hosting_country, created_at, cname, mx, ns, dns_status, dns_checked_at) FROM STDIN",
    "domain_source": "COPY domain_source (id, domain_id, source, first_seen, last_seen, batch_id) "
    "FROM STDIN",
    "store_profile": "COPY store_profile (host_id, platform_id, platform_confidence, "
    "platform_version, country, country_confidence, currency, vertical_id, "
    "vertical_confidence, checkout_status, coverage, checkout_country, acquirer_hidden, "
    "last_light_scan_at, last_checkout_scan_at, traffic_rank, updated_at) FROM STDIN",
    "store_provider": "COPY store_provider (id, host_id, provider_id, role, confidence, "
    "confidence_score, active_on_checkout, first_seen, last_seen, confirmations, misses, "
    "suppressed) FROM STDIN",
    "store_payment_method": "COPY store_payment_method (id, host_id, method_id, provider_id, "
    "confidence, confidence_score, first_seen, last_seen, confirmations, misses, suppressed) "
    "FROM STDIN",
    "store_checkout_host": "COPY store_checkout_host (id, host_id, third_party_etld1, category, "
    "provider_id, request_count, first_seen, last_seen, confirmations, misses) FROM STDIN",
    "scan_run": "COPY scan_run (id, host_id, scan_type, started_at, finished_at, status, "
    "coverage, stop_step, stop_reason, checkout_country, used_account, worker_id, "
    "ruleset_version, artifact_prefix, trace_key) FROM STDIN",
    "change_event": "COPY change_event (id, host_id, event_type, entity_type, entity_id, "
    "old_value, new_value, detected_at, scan_run_id, suppressed) FROM STDIN",
}
STORE_TABLES = (
    "change_event", "scan_run", "store_checkout_host", "store_payment_method", "store_provider",
    "store_profile", "domain_source", "host", "domain",
)  # fmt: skip
SEQUENCES = (
    ("domain", "id"), ("host", "id"), ("domain_source", "id"), ("store_provider", "id"),
    ("store_payment_method", "id"), ("store_checkout_host", "id"), ("change_event", "id"),
)  # fmt: skip


def _copy(conn: Any, table: str, rows: list[tuple[Any, ...]]) -> None:
    with conn.cursor() as cur, cur.copy(COPY_SQL[table]) as copy:
        for r in rows:
            copy.write_row(r)


def generate(
    engine: Engine,
    spec: SynthSpec,
    *,
    clock: Clock,
    pepper: str,
    key_prefix: str,
    reset: bool = False,
    reference: ReferenceData | None = None,
    progress: Callable[[str], None] | None = None,
) -> SynthResult:
    """Write the dataset and the client organisation; returns the counts and the raw key."""
    ref = reference or load_reference()
    now = clock.now()
    say = progress or (lambda _m: None)
    with Session(engine) as session:
        existing = session.execute(select(func.count()).select_from(StoreProfile)).scalar_one()
        if existing and not reset:
            raise RuntimeError(
                f"{existing} store_profile rows exist; pass reset=True to replace them (stand only)"
            )
        if reset:
            session.execute(
                text("TRUNCATE " + ", ".join(STORE_TABLES) + " RESTART IDENTITY CASCADE")
            )
            session.commit()
    gen = _Rows(ref, spec, now)
    counts = {"providers": 0, "methods": 0, "hosts": 0, "events": 0}
    raw = engine.raw_connection()
    try:
        conn = raw.driver_connection
        if conn is None:  # pragma: no cover - SQLAlchemy always sets it for psycopg
            raise RuntimeError("no driver connection")
        for start in range(1, spec.stores + 1, BATCH):
            stores = [gen.store(n) for n in range(start, min(start + BATCH, spec.stores + 1))]
            _copy(conn, "domain", [gen.domain(s) for s in stores])
            _copy(conn, "host", [gen.host(s) for s in stores])
            _copy(conn, "domain_source", [gen.source(s) for s in stores])
            _copy(conn, "store_profile", [gen.profile(s) for s in stores])
            prov = [r for s in stores for r in gen.provider_rows(s)]
            meth = [r for s in stores for r in gen.method_rows(s)]
            hosts = [r for s in stores for r in gen.host_rows(s)]
            events = [r for s in stores for r in gen.event_rows(s)]
            _copy(conn, "store_provider", prov)
            _copy(conn, "store_payment_method", meth)
            _copy(conn, "store_checkout_host", hosts)
            _copy(conn, "scan_run", [gen.scan_run(s) for s in stores])
            _copy(conn, "change_event", events)
            conn.commit()
            counts["providers"] += len(prov)
            counts["methods"] += len(meth)
            counts["hosts"] += len(hosts)
            counts["events"] += len(events)
            say(f"{min(start + BATCH - 1, spec.stores)}/{spec.stores} stores")
        with conn.cursor() as cur:
            for table, col in SEQUENCES:
                cur.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), "  # noqa: S608
                    f"COALESCE((SELECT max({col}) FROM {table}), 0) + 1, false)"
                )
            for table in STORE_TABLES:
                cur.execute(f"ANALYZE {table}")
        conn.commit()
    finally:
        raw.close()
    with Session(engine) as session:
        org_id, api_key = _client(session, now=now, clock=clock, pepper=pepper, prefix=key_prefix)
        session.commit()
    return SynthResult(
        stores=spec.stores,
        providers=counts["providers"],
        methods=counts["methods"],
        checkout_hosts=counts["hosts"],
        events=counts["events"],
        org_id=org_id,
        api_key=api_key,
    )


def _client(
    session: Session, *, now: datetime, clock: Clock, pepper: str, prefix: str
) -> tuple[uuid.UUID, str]:
    """One active organisation with an unrestricted `c1_full` entitlement and a key.
    The lifecycle (KYC, approval) is bypassed on purpose: this is a stand, not a client."""
    today: date = now.date()
    existing = session.execute(
        select(Organization).where(Organization.reg_number == "BENCH-0")
    ).scalar_one_or_none()
    if existing is not None:  # a previous run on this stand: keep the org, issue a fresh key
        return _issue(session, existing, now=now, clock=clock, pepper=pepper, prefix=prefix)
    org = Organization(
        legal_name="Bench Client GmbH",
        reg_number="BENCH-0",
        country="DE",
        status=OrgStatus.ACTIVE,
        created_at=now,
    )
    session.add(org)
    session.flush()
    contract = Contract(
        org_id=org.id,
        number="BENCH-1",
        product=Product.C_DATA,
        starts_on=today - timedelta(days=10),
        ends_on=today + timedelta(days=355),
        allowed_purposes=["market_research"],
        created_at=now,
    )
    session.add(contract)
    session.flush()
    session.add(
        Entitlement(
            contract_id=contract.id,
            countries=[],
            platforms=[],
            field_profile=FieldProfile.C1_FULL,
            api_rps=500,
            daily_records=100_000_000,
            monthly_records=2_000_000_000,
            export_max_rows=5_000_000,
            export_schedule="monthly",
            watchlist_limit=50,
            allowed_ips=[],
            active=True,
        )
    )
    return _issue(session, org, now=now, clock=clock, pepper=pepper, prefix=prefix)


def _issue(
    session: Session, org: Organization, *, now: datetime, clock: Clock, pepper: str, prefix: str
) -> tuple[uuid.UUID, str]:
    issued = keys_mod.issue_key(
        session,
        org_id=org.id,
        name=f"bench {now:%Y-%m-%d %H:%M}",
        scopes=sorted(SCOPES),
        expires_in_days=None,
        allowed_ips=[],
        created_by=None,
        actor="bench",
        pepper=pepper,
        prefix=prefix,
        clock=clock,
    )
    return org.id, issued.raw
