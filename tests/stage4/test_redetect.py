"""FR-DT-12: re-detect from stored artefacts (manifest + cleaned HTML) without a new scan."""

from __future__ import annotations

import gzip
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from payintel.core.clock import FixedClock
from payintel.core.models.audit import AuditLog
from payintel.core.models.base import ConfidenceLevel, ProviderRole, ScanStatus, ScanType
from payintel.core.models.scans import ScanRun
from payintel.core.models.store import StorePaymentMethod, StoreProvider
from payintel.core.reference_loader import load_reference
from payintel.core.s3 import ObjectStore
from payintel.core.settings import ClickHouseSettings, S3Settings
from payintel.crawl.light.runtime import provider_roles
from payintel.detect import redetect as rd
from payintel.detect.rules import load_rules
from tests.stage3.conftest import World

pytestmark = pytest.mark.integration

SHOPS = Path(__file__).resolve().parents[1] / "fixtures" / "shops"


def _put(store: ObjectStore, bucket: str, key: str, payload: bytes, ct: str) -> None:
    store.put_bytes(bucket, key, payload, content_type=ct)


def _light_run(
    session: Session, host_id: int, prefix: str, clock: FixedClock, store: ObjectStore, bucket: str
) -> ScanRun:
    html = (SHOPS / "woo-shop" / "index.html").read_text(encoding="utf-8")
    run = ScanRun(
        id=uuid.uuid4(),
        host_id=host_id,
        scan_type=ScanType.LIGHT,
        started_at=clock.now(),
        finished_at=clock.now(),
        status=ScanStatus.OK,
        worker_id="t",
        ruleset_version="old",
        artifact_prefix=prefix,
    )
    session.add(run)
    session.flush()
    _put(
        store,
        bucket,
        f"{prefix}pages/homepage.html.gz",
        gzip.compress(html.encode()),
        "application/gzip",
    )
    manifest = {
        "scan_run_id": str(run.id),
        "pages": [
            {
                "page_type": "homepage",
                "url": "https://alpha-shop.de/",
                "final_url": "https://alpha-shop.de/",
                "headers": {"server": "nginx", "set-cookie": "wp_woocommerce_session_x=1; Path=/"},
                "html_key": f"{prefix}pages/homepage.html.gz",
                "external_hosts": ["js.stripe.com"],
            },
            {
                "page_type": "cart",
                "url": "https://alpha-shop.de/cart",
                "headers": {},
                "scripts": [],
            },
        ],
    }
    _put(store, bucket, f"{prefix}manifest.json", json.dumps(manifest).encode(), "application/json")
    return run


def _checkout_run(
    session: Session, host_id: int, prefix: str, clock: FixedClock, store: ObjectStore, bucket: str
) -> ScanRun:
    run = ScanRun(
        id=uuid.uuid4(),
        host_id=host_id,
        scan_type=ScanType.CHECKOUT,
        started_at=clock.now(),
        finished_at=clock.now(),
        status=ScanStatus.REACHED_PAYMENT_STEP,
        worker_id="t",
        ruleset_version="old",
        artifact_prefix=prefix,
    )
    session.add(run)
    session.flush()
    html = (
        "<html><body><form action='/checkout'><label>Kreditkarte</label><label>PayPal</label>"
        "<iframe src='https://js.stripe.com/v3/elements-inner-card.html'></iframe>"
        "</form></body></html>"
    )
    _put(
        store,
        bucket,
        f"{prefix}payment_step.html.gz",
        gzip.compress(html.encode()),
        "application/gzip",
    )
    manifest = {
        "scan_run_id": str(run.id),
        "scan_type": "checkout",
        "checkout_url": "https://alpha-shop.de/checkout",
        "network": [
            {"url": "https://api.stripe.com/v1/tokens", "host": "api.stripe.com"},
            {"url": "https://www.paypal.com/sdk/js", "host": "www.paypal.com"},
        ],
        "third_party_hosts": [{"host": "m.stripe.network", "etld1": "stripe.network"}],
        "main_headers": {"content-type": "text/html"},
        "cookie_names": ["__stripe_mid"],
        "capture": {
            "url": "https://alpha-shop.de/checkout",
            "html_key": f"{prefix}payment_step.html.gz",
            "js_globals": ["Stripe"],
            "labels": ["Kreditkarte", "PayPal"],
        },
    }
    _put(store, bucket, f"{prefix}manifest.json", json.dumps(manifest).encode(), "application/json")
    return run


def test_redetect_rebuilds_signals_from_artifacts_and_applies(
    db_session: Session,
    world: World,
    fixed_clock: FixedClock,
    object_store: ObjectStore,
    s3_settings: S3Settings,
) -> None:
    host = world.hosts["alpha-shop.de"]  # seeded with adyen only
    bucket = s3_settings.bucket_artifacts
    reader = rd.ArtifactReader(object_store, bucket)
    assert reader.json("missing/manifest.json") is None and reader.html("missing.html.gz") == ""
    reference = load_reference()
    ruleset = load_rules(reference=reference)
    roles = provider_roles(reference)
    empty = rd.redetect_host(
        db_session,
        reader,
        ruleset,
        host.id,
        provider_roles=roles,
        apply=False,
        actor="t",
        clock=fixed_clock,
    )
    assert empty.pages == 0 and empty.note and not empty.changes
    with pytest.raises(ValueError):
        rd.redetect_host(
            db_session,
            reader,
            ruleset,
            10**9,
            provider_roles=roles,
            apply=False,
            actor="t",
            clock=fixed_clock,
        )
    _light_run(db_session, host.id, "rt/light/1/", fixed_clock, object_store, bucket)
    fixed_clock.advance(seconds=60)
    _checkout_run(db_session, host.id, "rt/checkout/1/", fixed_clock, object_store, bucket)
    # an older run without artefacts is ignored, the newest per type is used
    old = _light_run(db_session, host.id, "rt/light/0/", fixed_clock, object_store, bucket)
    old.started_at = fixed_clock.now().replace(year=2020)
    db_session.flush()

    dry = rd.redetect_host(
        db_session,
        reader,
        ruleset,
        host.id,
        provider_roles=roles,
        apply=False,
        actor="t",
        clock=fixed_clock,
    )
    assert len(dry.runs) == 2 and dry.pages == 3 and not dry.applied
    providers = {s.target_id: s for s in dry.providers}
    assert "stripe" in providers and providers["stripe"].active_on_checkout
    assert providers["stripe"].confidence in (ConfidenceLevel.MEDIUM, ConfidenceLevel.HIGH)
    assert "paypal" in providers
    kinds = {(c.entity_type, c.entity_id, c.kind) for c in dry.changes}
    assert ("provider", "stripe", "added") in kinds and ("provider", "adyen", "removed") in kinds
    assert db_session.execute(
        select(StoreProvider.provider_id).where(StoreProvider.host_id == host.id)
    ).scalars().all() == ["adyen"]
    csv_text = rd.to_csv([dry])
    assert csv_text.startswith("domain,entity_type,entity_id,confidence,confidence_score,rule_id")
    assert "alpha-shop.de,provider,stripe," in csv_text
    line = rd.summary_line(dry)
    assert line.startswith("alpha-shop.de: runs=2 pages=3") and "applied" not in line

    applied = rd.redetect_host(
        db_session,
        reader,
        ruleset,
        host.id,
        provider_roles=roles,
        apply=True,
        actor="staff_analyst:a",
        clock=fixed_clock,
    )
    assert applied.applied and "applied" in rd.summary_line(applied)
    rows = {
        r.provider_id: r
        for r in db_session.execute(
            select(StoreProvider).where(StoreProvider.host_id == host.id)
        ).scalars()
    }
    assert (
        "stripe" in rows
        and rows["stripe"].active_on_checkout
        and rows["stripe"].role == roles["stripe"]
    )
    assert "adyen" in rows  # never deleted by a re-detect; removal goes through the differ
    methods = set(
        db_session.execute(
            select(StorePaymentMethod.method_id).where(StorePaymentMethod.host_id == host.id)
        ).scalars()
    )
    assert "paypal" in methods or "visa" in methods
    audit_rows = (
        db_session.execute(select(AuditLog).where(AuditLog.action == "detect.redetect"))
        .scalars()
        .all()
    )
    assert (
        len(audit_rows) == 1 and (audit_rows[0].after or {})["ruleset_version"] == ruleset.version
    )
    assert (
        rd.candidate_hosts(db_session, limit=10, since_days=None, now=fixed_clock.now())[0]
        == host.id
    )
    assert rd.candidate_hosts(db_session, limit=10, since_days=1, now=fixed_clock.now()) == [
        host.id
    ]
    assert rd.latest_runs(db_session, host.id, since=fixed_clock.now().replace(year=2030)) == []
    # light-only host: providers are upserted with low confidence and not active on checkout
    beta = world.hosts["beta-store.de"]
    _light_run(db_session, beta.id, "rt/light/beta/", fixed_clock, object_store, bucket)
    r = rd.redetect_host(
        db_session,
        reader,
        ruleset,
        beta.id,
        provider_roles=roles,
        apply=True,
        actor="t",
        clock=fixed_clock,
    )
    assert r.applied and any(s.target_id == "stripe" for s in r.providers)
    assert ProviderRole.GATEWAY in {rr.role for rr in rows.values()}


def test_redetect_cli_dry_run_and_csv(
    tmp_path: Path,
    fresh_database: str,
    ch_settings: ClickHouseSettings,
    s3_settings: S3Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from payintel.cli import app
    from payintel.core.db import get_engine
    from payintel.core.settings import get_settings

    env = {
        "PAYINTEL_POSTGRES__DSN": fresh_database,
        "PAYINTEL_CLICKHOUSE__URL": ch_settings.url,
        "PAYINTEL_CLICKHOUSE__USER": ch_settings.user,
        "PAYINTEL_CLICKHOUSE__PASSWORD": ch_settings.password.get_secret_value(),
        "PAYINTEL_CLICKHOUSE__DATABASE": ch_settings.database,
        "PAYINTEL_S3__ENDPOINT": s3_settings.endpoint,
        "PAYINTEL_S3__ACCESS_KEY": s3_settings.access_key.get_secret_value(),
        "PAYINTEL_S3__SECRET_KEY": s3_settings.secret_key.get_secret_value(),
        "PAYINTEL_S3__BUCKET_ARTIFACTS": s3_settings.bucket_artifacts,
        "PAYINTEL_S3__BUCKET_EXPORTS": s3_settings.bucket_exports,
        "PAYINTEL_SECRETS__ENCRYPTION_KEY": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=",
        "PAYINTEL_SECRETS__API_KEY_PEPPER": "p",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PAYINTEL_ALEMBIC_DSN", raising=False)
    get_settings.cache_clear()
    get_engine.cache_clear()
    runner = CliRunner()
    try:
        for args in (["migrate"], ["seed"]):
            assert runner.invoke(app, args).exit_code == 0
        out = tmp_path / "findings.csv"
        result = runner.invoke(app, ["redetect", "--limit", "5", "--csv", str(out)])
        assert result.exit_code == 0, (result.output, result.exception)
        assert "0 host(s), 0 with changes (dry run)" in result.output
        assert out.read_text().startswith("domain,entity_type")
        result = runner.invoke(app, ["redetect", "--host", "nope.example", "--apply"])
        assert result.exit_code == 0 and "(applied)" in result.output
    finally:
        get_engine().dispose()
        get_engine.cache_clear()
        get_settings.cache_clear()
