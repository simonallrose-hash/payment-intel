"""Import of detection results from CSV for evaluation runs.

Used by CI (`tests/fixtures/gold/sample_findings.csv`) and by analysts who want
to score a detector output produced elsewhere (e.g. a re-detect dry run,
FR-DT-12) without running the scanners. Rows are written to the same places the
detector writes to: `store_provider` / `store_payment_method` / `store_profile`
in Postgres and, when a ClickHouse client is given, `obs_provider` /
`obs_payment_method` for per-rule metrics.

CSV columns: `domain,entity_type,entity_id,confidence,confidence_score,rule_id,rule_version,
active_on_checkout`.
"""

from __future__ import annotations

import csv
import uuid
from dataclasses import dataclass
from pathlib import Path

from clickhouse_connect.driver.client import Client
from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.errors import ValidationError
from payintel.core.models.base import ConfidenceLevel, GoldEntityType
from payintel.core.models.reference import PaymentMethod, Platform, Provider
from payintel.core.models.store import StorePaymentMethod, StoreProfile, StoreProvider
from payintel.quality.gold import get_or_create_host

REQUIRED_COLUMNS = ("domain", "entity_type", "entity_id", "confidence")


@dataclass
class FindingsImportResult:
    providers: int = 0
    methods: int = 0
    platforms: int = 0
    countries: int = 0
    observations: int = 0


def import_findings_csv(
    session: Session,
    path: Path,
    *,
    ch: Client | None = None,
    clock: Clock = SYSTEM_CLOCK,
) -> FindingsImportResult:
    result = FindingsImportResult()
    providers = {p.id: p for p in session.execute(select(Provider)).scalars()}
    methods = set(session.execute(select(PaymentMethod.id)).scalars())
    platforms = set(session.execute(select(Platform.id)).scalars())
    now = clock.now()
    today = now.date()
    scan_run_id = uuid.uuid4()
    obs_provider_rows: list[list[object]] = []
    obs_method_rows: list[list[object]] = []

    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValidationError(f"{path.name}: missing columns {missing}")
        for line_no, row in enumerate(reader, start=2):
            entity_type = GoldEntityType(row["entity_type"].strip())
            entity_id = row["entity_id"].strip()
            confidence = ConfidenceLevel(row["confidence"].strip())
            score = float(
                row.get("confidence_score")
                or {"high": 0.9, "medium": 0.6, "low": 0.3}[confidence.value]
            )
            rule_id = (row.get("rule_id") or "").strip()
            rule_version = int(row.get("rule_version") or 1)
            active = (row.get("active_on_checkout") or "true").strip().lower() in {
                "true",
                "1",
                "yes",
            }
            host = get_or_create_host(session, row["domain"])
            profile = session.get(StoreProfile, host.id)
            if profile is None:
                profile = StoreProfile(host_id=host.id)
                session.add(profile)
                session.flush()

            if entity_type == GoldEntityType.PROVIDER:
                provider = providers.get(entity_id)
                if provider is None:
                    raise ValidationError(f"line {line_no}: unknown provider {entity_id!r}")
                sp = session.execute(
                    select(StoreProvider).where(
                        StoreProvider.host_id == host.id, StoreProvider.provider_id == entity_id
                    )
                ).scalar_one_or_none()
                if sp is None:
                    session.add(
                        StoreProvider(
                            host_id=host.id,
                            provider_id=entity_id,
                            role=provider.role,
                            confidence=confidence,
                            confidence_score=score,
                            active_on_checkout=active,
                            first_seen=today,
                            last_seen=today,
                        )
                    )
                else:
                    sp.confidence, sp.confidence_score, sp.last_seen = confidence, score, today
                result.providers += 1
                if rule_id:
                    obs_provider_rows.append(
                        [
                            today,
                            now,
                            host.id,
                            host.hostname,
                            entity_id,
                            provider.role.value,
                            "imported",
                            "",
                            "checkout",
                            "",
                            rule_id,
                            rule_version,
                            confidence.value,
                            score,
                            int(active),
                            "",
                            scan_run_id,
                            "",
                            "",
                            "",
                        ]
                    )
            elif entity_type == GoldEntityType.PAYMENT_METHOD:
                if entity_id not in methods:
                    raise ValidationError(f"line {line_no}: unknown payment method {entity_id!r}")
                spm = session.execute(
                    select(StorePaymentMethod).where(
                        StorePaymentMethod.host_id == host.id,
                        StorePaymentMethod.method_id == entity_id,
                    )
                ).scalar_one_or_none()
                if spm is None:
                    session.add(
                        StorePaymentMethod(
                            host_id=host.id,
                            method_id=entity_id,
                            confidence=confidence,
                            confidence_score=score,
                            first_seen=today,
                            last_seen=today,
                        )
                    )
                else:
                    spm.confidence, spm.confidence_score, spm.last_seen = confidence, score, today
                result.methods += 1
                if rule_id:
                    obs_method_rows.append(
                        [
                            today,
                            now,
                            host.id,
                            host.hostname,
                            entity_id,
                            "",
                            "imported",
                            "",
                            "checkout",
                            "",
                            rule_id,
                            rule_version,
                            confidence.value,
                            score,
                            "",
                            scan_run_id,
                            "",
                            "",
                            "",
                        ]
                    )
            elif entity_type == GoldEntityType.PLATFORM:
                if entity_id not in platforms:
                    raise ValidationError(f"line {line_no}: unknown platform {entity_id!r}")
                profile.platform_id = entity_id
                profile.platform_confidence = confidence
                result.platforms += 1
            else:
                profile.country = entity_id
                profile.country_confidence = confidence
                result.countries += 1
    session.flush()

    if ch is not None:
        if obs_provider_rows:
            ch.insert(
                "obs_provider",
                obs_provider_rows,
                column_names=[
                    "scan_date",
                    "scan_ts",
                    "host_id",
                    "etld1",
                    "provider_id",
                    "role",
                    "signal_type",
                    "signal_value",
                    "page_type",
                    "page_url",
                    "rule_id",
                    "rule_version",
                    "confidence",
                    "confidence_score",
                    "active_on_checkout",
                    "evidence_key",
                    "scan_run_id",
                    "country",
                    "platform_id",
                    "vertical_id",
                ],
            )
        if obs_method_rows:
            ch.insert(
                "obs_payment_method",
                obs_method_rows,
                column_names=[
                    "scan_date",
                    "scan_ts",
                    "host_id",
                    "etld1",
                    "method_id",
                    "provider_id",
                    "signal_type",
                    "signal_value",
                    "page_type",
                    "page_url",
                    "rule_id",
                    "rule_version",
                    "confidence",
                    "confidence_score",
                    "evidence_key",
                    "scan_run_id",
                    "country",
                    "platform_id",
                    "vertical_id",
                ],
            )
        result.observations = len(obs_provider_rows) + len(obs_method_rows)
    return result
