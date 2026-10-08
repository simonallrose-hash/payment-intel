"""Light scan worker (4.3): robots → homepage → ≤2 product pages → cart → artefacts → detection.

One `LightScanner` serves many concurrent scans (asyncio); each scan is one
leased `scan_plan` row and ends in exactly one `scan_run` whose id is derived
from the lease (idempotent retry, NFR-R-05). The scanner only *collects*
artefacts and applies rules on them (6.5 decision 1); nothing here clicks,
submits forms or executes JavaScript.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.logging import get_logger, log_context
from payintel.core.models.base import (
    ConfidenceLevel,
    Coverage,
    DomainSourceKind,
    DomainStatus,
    ProviderRole,
    RuleTargetType,
    ScanStatus,
    ScanType,
)
from payintel.core.models.domains import Domain, Host
from payintel.core.models.scans import ScanPlan, ScanRun
from payintel.core.settings import Settings
from payintel.crawl.light.artifacts import ArtifactWriter, record_script, sha256_hex
from payintel.crawl.light.fetcher import Fetcher, FetchResult
from payintel.crawl.light.html import HtmlFeatures, extract
from payintel.crawl.light.robots import RobotsRules, agent_token, parse_robots
from payintel.crawl.sanitize import SanitizeStats, sanitize_headers, sanitize_text
from payintel.detect.country import CountryDetector, CountryResult
from payintel.detect.engine import Finding, PageSignals, match_page
from payintel.detect.rules import RuleSet
from payintel.detect.scoring import TargetScore, aggregate, best_platform
from payintel.discovery.classify import Classification, ClassifierInput, EcommerceClassifier
from payintel.discovery.ingest import ingest
from payintel.discovery.parking import ParkingDetector
from payintel.discovery.sources.base import SourceRecord
from payintel.history.materialize import (
    ProfileUpdate,
    ProviderObservation,
    upsert_profile,
    upsert_providers,
)
from payintel.history.writer import ObservationBuffer
from payintel.scheduler import queue
from payintel.scheduler.planner import interval_for

log = get_logger("payintel.crawl.light")

PLATFORM_CART_PATHS: dict[str, str] = {
    "woocommerce": "/cart/",
    "shopify": "/cart",
    "magento2": "/checkout/cart/",
    "magento1": "/checkout/cart/",
    "prestashop": "/cart?action=show",
    "shopware6": "/checkout/cart",
    "shopware5": "/checkout/cart",
    "opencart": "/index.php?route=checkout/cart",
    "bigcommerce": "/cart.php",
}
BLOCKED_STATUSES = {401, 403, 429, 503}
MAX_SCRIPTS = 25
MAX_LINK_CANDIDATES = 50
ROBOTS_MAX_BYTES = 512 * 1024
_GENERATOR_VERSION_RE = re.compile(
    r"^(?P<name>[A-Za-z][\w .-]*?)\s*v?(?P<version>\d+(?:\.\d+){1,3})\b"
)


@dataclass
class PageCapture:
    page_type: str
    fetch: FetchResult
    features: HtmlFeatures | None = None
    raw_html: str = ""
    clean_html: str = ""
    sanitize: SanitizeStats = field(default_factory=SanitizeStats)
    findings: list[Finding] = field(default_factory=list)


@dataclass
class LightScanOutcome:
    scan_run_id: uuid.UUID
    host_id: int
    etld1: str
    status: ScanStatus
    coverage: Coverage | None
    stop_reason: str | None
    pages: list[PageCapture]
    scores: list[TargetScore]
    platform: TargetScore | None
    platform_version: str | None
    country: CountryResult | None
    classification: Classification | None
    parked_by: str | None
    scripts: dict[str, str]  # src url → sha256
    artifact_prefix: str
    duration_ms: int
    domain_status: DomainStatus | None = None
    new_link_candidates: int = 0

    @property
    def providers(self) -> list[TargetScore]:
        return [s for s in self.scores if s.target_type == RuleTargetType.PROVIDER]


@dataclass
class ScanContext:
    settings: Settings
    ruleset: RuleSet
    provider_roles: dict[str, ProviderRole]
    classifier: EcommerceClassifier
    country: CountryDetector
    parking: ParkingDetector
    fetcher: Fetcher
    buffer: ObservationBuffer
    writer: ArtifactWriter
    clock: Clock = SYSTEM_CLOCK
    worker_id: str = "worker-light-0"
    base_scheme: str = "https"

    @property
    def agent_token(self) -> str:
        return agent_token(self.settings.identity.user_agent)


def _page_signals(cap: PageCapture) -> PageSignals:
    f = cap.features
    if f is None:
        return PageSignals(page_type=cap.page_type, url=cap.fetch.final_url)
    cookies = set()
    for k, v in cap.fetch.headers.items():
        if k == "set-cookie":
            for part in v.split("\n"):
                name = part.split("=", 1)[0].strip()
                if name:
                    cookies.add(name)
    return PageSignals(
        page_type=cap.page_type,
        url=cap.fetch.final_url,
        html=cap.raw_html,
        headers=cap.fetch.headers,
        cookie_names=cookies,
        script_srcs=f.scripts_src,
        iframe_srcs=f.iframes_src,
        form_actions=f.forms_action,
        network_hosts=f.external_hosts(),
        js_globals=f.js_globals(),
    )


def _version_from_generator(meta: dict[str, str], platform_id: str | None) -> str | None:
    gen = meta.get("generator", "")
    m = _GENERATOR_VERSION_RE.match(gen.strip())
    if not m or platform_id is None:
        return None
    name = m.group("name").lower().replace(" ", "")
    if platform_id.replace("2", "").replace("1", "") in name or name in platform_id:
        return m.group("version")
    return None


class LightScanner:
    def __init__(self, ctx: ScanContext) -> None:
        self.ctx = ctx

    # --- fetching helpers -------------------------------------------------------
    async def _robots(self, base: str) -> RobotsRules:
        r = await self.ctx.fetcher.fetch(urljoin(base, "/robots.txt"), max_bytes=ROBOTS_MAX_BYTES)
        body = r.text() if r.error is None and r.status == 200 else None
        return parse_robots(body, None if r.error else r.status, agent_token=self.ctx.agent_token)

    async def _capture(self, url: str, page_type: str) -> PageCapture:
        fetch = await self.ctx.fetcher.fetch(url)
        cap = PageCapture(page_type=page_type, fetch=fetch)
        if fetch.error is None and fetch.status is not None and fetch.status < 400 and fetch.body:
            cap.raw_html = fetch.text()
            cap.features = extract(cap.raw_html, fetch.final_url)
            cap.clean_html, cap.sanitize = sanitize_text(cap.raw_html)
            cap.findings = match_page(self.ctx.ruleset, _page_signals(cap))
        return cap

    def _same_host(self, url: str, hostname: str) -> bool:
        h = urlsplit(url).hostname or ""
        return h.lower() in {hostname, "www." + hostname}

    def _pick_product_urls(
        self, home: PageCapture, hostname: str, robots: RobotsRules
    ) -> list[str]:
        if home.features is None:
            return []
        out: list[str] = []
        for link in home.features.links:
            path = urlsplit(link).path.lower()
            if not self._same_host(link, hostname) or not robots.allows(link):
                continue
            if any(p in path for p in self.ctx.classifier.product_paths) and link not in out:
                if path.rstrip("/") and path.rstrip("/").count("/") >= 1:
                    out.append(link)
            if len(out) >= self.ctx.settings.light.max_product_pages:
                break
        return out

    def _pick_cart_url(
        self, home: PageCapture, hostname: str, platform_id: str | None, robots: RobotsRules
    ) -> str | None:
        base = home.fetch.final_url
        if home.features is not None:
            for link in home.features.links:
                path = urlsplit(link).path.lower()
                if self._same_host(link, hostname) and any(
                    cp in path for cp in self.ctx.classifier.cart_paths
                ):
                    return link if robots.allows(link) else None
        if platform_id in PLATFORM_CART_PATHS:
            url = urljoin(base, PLATFORM_CART_PATHS[platform_id])
            return url if robots.allows(url) else None
        return None

    async def _download_scripts(
        self, session: Session, host: Host, pages: list[PageCapture], now: datetime
    ) -> dict[str, str]:
        seen: list[str] = []
        for cap in pages:
            if cap.features is None:
                continue
            for src in cap.features.scripts_src:
                if src not in seen and urlsplit(src).scheme in {"http", "https"}:
                    seen.append(src)
        result: dict[str, str] = {}
        for src in seen[:MAX_SCRIPTS]:
            r = await self.ctx.fetcher.fetch(src, max_bytes=self.ctx.settings.light.max_page_bytes)
            if r.error or r.status != 200 or not r.body or r.truncated:
                continue
            digest, _new = record_script(
                session,
                self.ctx.writer,
                host_id=host.id,
                src_url=src,
                body=r.body,
                content_type=r.headers.get("content-type"),
                now=now,
            )
            result[src] = digest
        return result

    # --- main ---------------------------------------------------------------------
    async def scan(self, session: Session, plan: ScanPlan) -> LightScanOutcome:
        host = session.get(Host, plan.host_id)
        if host is None:
            raise LookupError(f"plan {plan.id} without host")
        domain = session.get(Domain, host.domain_id)
        if domain is None:
            raise LookupError(f"host {host.id} without domain")
        run_id = queue.run_id_for(plan)
        started = self.ctx.clock.now()
        t0 = asyncio.get_running_loop().time()
        prefix = f"light/{domain.etld1}/{run_id}/"
        with log_context(scan_run_id=str(run_id)):
            outcome = await self._scan_pages(session, host, domain, run_id, prefix)
            outcome.duration_ms = int((asyncio.get_running_loop().time() - t0) * 1000)
            self._persist(session, host, domain, plan, outcome, started)
        return outcome

    async def _scan_pages(
        self, session: Session, host: Host, domain: Domain, run_id: uuid.UUID, prefix: str
    ) -> LightScanOutcome:
        s = self.ctx.settings
        base = f"{self.ctx.base_scheme}://{host.hostname}/"
        empty = LightScanOutcome(
            run_id,
            host.id,
            domain.etld1,
            ScanStatus.ERROR,
            None,
            None,
            [],
            [],
            None,
            None,
            None,
            None,
            None,
            {},
            prefix,
            0,
        )
        robots = await self._robots(base)
        if not robots.allows(base):
            empty.status, empty.stop_reason = (
                ScanStatus.BLOCKED,
                "robots_disallow" if robots.fetched else "robots_unavailable",
            )
            return empty
        home = await self._capture(base, "homepage")
        pages = [home]
        empty.pages = pages
        f = home.fetch
        if f.error is not None:
            empty.status = ScanStatus.TIMEOUT if "timeout" in f.error else ScanStatus.ERROR
            empty.stop_reason = f.error[:64]
            return empty
        if f.status in BLOCKED_STATUSES:
            empty.status, empty.stop_reason = ScanStatus.BLOCKED, f"http_{f.status}"
            return empty
        if f.status is None or f.status >= 400 or home.features is None:
            empty.status, empty.stop_reason = ScanStatus.ERROR, f"http_{f.status}"
            return empty

        scores = aggregate(home.findings)
        platform = best_platform(scores)
        platform_id = platform.target_id if platform else None
        parked = self.ctx.parking.check_html(
            home.raw_html,
            link_count=len(home.features.links),
            max_stub_bytes=s.discovery.parking_max_page_bytes,
        )
        if not parked.parked:
            for url in self._pick_product_urls(home, host.hostname, robots):
                pages.append(await self._capture(url, "product"))
            cart_url = self._pick_cart_url(home, host.hostname, platform_id, robots)
            if cart_url:
                pages.append(await self._capture(cart_url, "cart"))
        findings = [fd for p in pages for fd in p.findings]
        scores = aggregate(findings)
        platform = best_platform(scores)
        platform_id = platform.target_id if platform else None
        now = self.ctx.clock.now()
        scripts = await self._download_scripts(session, host, pages, now)
        country = self.ctx.country.detect(
            home.features,
            etld1=domain.etld1,
            raw_text=home.features.text,
            hosting_country=host.hosting_country,
        )
        classification = self.ctx.classifier.classify(
            ClassifierInput(
                home.features,
                platform_id=platform_id if platform_id not in {"wordpress"} else None,
                platform_confidence=platform.confidence if platform else None,
            )
        )
        coverage = Coverage.HOMEPAGE
        if any(p.page_type == "cart" and p.features is not None for p in pages):
            coverage = Coverage.CART
        elif any(p.page_type == "product" and p.features is not None for p in pages):
            coverage = Coverage.PRODUCT
        return LightScanOutcome(
            scan_run_id=run_id,
            host_id=host.id,
            etld1=domain.etld1,
            status=ScanStatus.OK,
            coverage=coverage,
            stop_reason=None,
            pages=pages,
            scores=scores,
            platform=platform,
            platform_version=_version_from_generator(home.features.meta, platform_id),
            country=country,
            classification=classification,
            parked_by=parked.signature_id if parked.parked else None,
            scripts=scripts,
            artifact_prefix=prefix,
            duration_ms=0,
        )

    # --- persistence --------------------------------------------------------------
    def _persist(
        self,
        session: Session,
        host: Host,
        domain: Domain,
        plan: ScanPlan,
        o: LightScanOutcome,
        started: datetime,
    ) -> None:
        now = self.ctx.clock.now()
        existing = session.get(ScanRun, o.scan_run_id)
        if existing is None:
            session.add(
                ScanRun(
                    id=o.scan_run_id,
                    host_id=host.id,
                    scan_type=ScanType.LIGHT,
                    started_at=started,
                    finished_at=now,
                    status=o.status,
                    coverage=o.coverage,
                    stop_step=None if o.status == ScanStatus.OK else "homepage",
                    stop_reason=o.stop_reason,
                    worker_id=self.ctx.worker_id,
                    ruleset_version=self.ctx.ruleset.version,
                    artifact_prefix=o.artifact_prefix if o.status == ScanStatus.OK else None,
                )
            )
            session.flush()
        self._write_artifacts(o)
        self._write_observations(host, domain, o, now)
        if o.status == ScanStatus.OK:
            self._apply_profile(session, host, domain, o, now)
            o.new_link_candidates = self._feed_links(session, host, o)
        self._reschedule(session, domain, plan, o, now)
        log.info(
            "light scan finished",
            host=host.hostname,
            status=o.status.value,
            stop_reason=o.stop_reason,
            platform=o.platform.target_id if o.platform else None,
            providers=[p.target_id for p in o.providers],
            pages=len(o.pages),
            duration_ms=o.duration_ms,
        )

    def _write_artifacts(self, o: LightScanOutcome) -> None:
        if o.status != ScanStatus.OK:
            return
        manifest: dict[str, Any] = {
            "scan_run_id": str(o.scan_run_id),
            "etld1": o.etld1,
            "ruleset_version": self.ctx.ruleset.version,
            "scanner_version": self.ctx.settings.scanner_version,
            "pages": [],
            "scripts": o.scripts,
        }
        for p in o.pages:
            headers, _ = sanitize_headers(p.fetch.headers)
            entry: dict[str, Any] = {
                "page_type": p.page_type,
                "url": p.fetch.url,
                "final_url": p.fetch.final_url,
                "status": p.fetch.status,
                "elapsed_ms": p.fetch.elapsed_ms,
                "bytes": p.fetch.bytes_read,
                "truncated": p.fetch.truncated,
                "hops": p.fetch.hops,
                "headers": headers,
                "tls": p.fetch.tls.__dict__ if p.fetch.tls else None,
                "scripts": p.features.scripts_src if p.features else [],
                "iframes": p.features.iframes_src if p.features else [],
                "external_hosts": sorted(p.features.external_hosts()) if p.features else [],
                "redacted": {"emails": p.sanitize.emails, "phones": p.sanitize.phones},
                "findings": [
                    {"rule_id": f.rule.rule_id, "version": f.rule.version, "value": f.signal_value}
                    for f in p.findings
                ],
            }
            if p.clean_html:
                key = f"{o.artifact_prefix}pages/{p.page_type}.html.gz"
                entry["html_key"] = key
                entry["html_sha256"] = sha256_hex(p.clean_html.encode("utf-8"))
                self.ctx.writer.put_html(key, p.clean_html)
            manifest["pages"].append(entry)
        self.ctx.writer.put_json(f"{o.artifact_prefix}manifest.json", manifest)

    def _write_observations(
        self, host: Host, domain: Domain, o: LightScanOutcome, now: datetime
    ) -> None:
        buf = self.ctx.buffer
        platform_id = o.platform.target_id if o.platform else ""
        country = o.country.country if o.country and o.country.country else ""
        buf.add(
            "obs_scan",
            {
                "scan_date": now.date(),
                "scan_ts": now,
                "scan_run_id": o.scan_run_id,
                "host_id": host.id,
                "etld1": domain.etld1,
                "scan_type": "light",
                "status": o.status.value,
                "coverage": o.coverage.value if o.coverage else "",
                "duration_ms": o.duration_ms,
                "blocked_by": o.stop_reason or "" if o.status == ScanStatus.BLOCKED else "",
                "platform_id": platform_id,
                "adapter": "light",
                "scanner_version": self.ctx.settings.scanner_version,
                "ruleset_version": self.ctx.ruleset.version,
            },
        )
        for sc in o.scores:
            if sc.target_type in {RuleTargetType.PLATFORM, RuleTargetType.TECH}:
                for f in sc.findings:
                    buf.add(
                        "obs_tech",
                        {
                            "scan_date": now.date(),
                            "scan_ts": now,
                            "host_id": host.id,
                            "tech_id": sc.target_id,
                            "version": (o.platform_version or "") if sc is o.platform else "",
                            "confidence": sc.confidence.value,
                            "confidence_score": sc.score,
                            "rule_id": f.rule.rule_id,
                            "rule_version": f.rule.version,
                            "page_type": f.page_type,
                            "scan_run_id": o.scan_run_id,
                        },
                    )
            elif sc.target_type == RuleTargetType.PROVIDER:
                role = self.ctx.provider_roles.get(sc.target_id, ProviderRole.GATEWAY)
                for f in sc.findings:
                    buf.add(
                        "obs_provider",
                        {
                            "scan_date": now.date(),
                            "scan_ts": now,
                            "host_id": host.id,
                            "etld1": domain.etld1,
                            "provider_id": sc.target_id,
                            "role": role.value,
                            "signal_type": f.rule.signal_type.value,
                            "signal_value": f.signal_value,
                            "page_type": f.page_type,
                            "page_url": f.page_url,
                            "rule_id": f.rule.rule_id,
                            "rule_version": f.rule.version,
                            "confidence": ConfidenceLevel.LOW.value,
                            "confidence_score": sc.score,
                            "active_on_checkout": 0,
                            "evidence_key": f"{o.artifact_prefix}manifest.json",
                            "scan_run_id": o.scan_run_id,
                            "country": country,
                            "platform_id": platform_id,
                            "vertical_id": "",
                        },
                    )
            elif sc.target_type == RuleTargetType.PAYMENT_METHOD:
                for f in sc.findings:
                    buf.add(
                        "obs_payment_method",
                        {
                            "scan_date": now.date(),
                            "scan_ts": now,
                            "host_id": host.id,
                            "etld1": domain.etld1,
                            "method_id": sc.target_id,
                            "provider_id": "",
                            "signal_type": f.rule.signal_type.value,
                            "signal_value": f.signal_value,
                            "page_type": f.page_type,
                            "page_url": f.page_url,
                            "rule_id": f.rule.rule_id,
                            "rule_version": f.rule.version,
                            "confidence": ConfidenceLevel.LOW.value,
                            "confidence_score": sc.score,
                            "evidence_key": f"{o.artifact_prefix}manifest.json",
                            "scan_run_id": o.scan_run_id,
                            "country": country,
                            "platform_id": platform_id,
                            "vertical_id": "",
                        },
                    )

    def _apply_profile(
        self, session: Session, host: Host, domain: Domain, o: LightScanOutcome, now: datetime
    ) -> None:
        platform = o.platform
        upsert_profile(
            session,
            host.id,
            ProfileUpdate(
                platform_id=platform.target_id if platform else None,
                platform_confidence=platform.confidence if platform else None,
                platform_version=o.platform_version,
                country=o.country.country if o.country else None,
                country_confidence=o.country.confidence if o.country else None,
                currency=o.country.currency if o.country else None,
                traffic_rank=domain.traffic_rank,
            ),
            scanned_at=now,
        )
        upsert_providers(
            session,
            host.id,
            [
                ProviderObservation(
                    provider_id=p.target_id,
                    role=self.ctx.provider_roles.get(p.target_id, ProviderRole.GATEWAY),
                    confidence=ConfidenceLevel.LOW,
                    score=p.score,
                    active_on_checkout=False,
                )
                for p in o.providers
            ],
            scanned_at=now,
        )
        if not host.is_primary or domain.status == DomainStatus.OPTOUT:
            return
        if o.parked_by:
            new_status, reason = DomainStatus.PARKED, o.parked_by
            domain.ecommerce_confidence = 0.0
        elif o.classification is not None:
            new_status, reason = o.classification.status, "classifier"
            domain.ecommerce_confidence = o.classification.score
        else:
            return
        if domain.status != new_status:
            domain.status = new_status
            domain.status_reason = reason
            domain.status_changed_at = now
        o.domain_status = domain.status

    def _feed_links(self, session: Session, host: Host, o: LightScanOutcome) -> int:
        """FR-LS-06: external domains linked from the pages become discovery candidates."""
        hosts: set[str] = set()
        for p in o.pages:
            if p.features is not None:
                hosts |= p.features.external_link_hosts()
        hosts.discard(host.hostname)
        if not hosts:
            return 0
        records = [SourceRecord(h) for h in sorted(hosts)[:MAX_LINK_CANDIDATES]]
        result = ingest(
            session,
            records,
            source=DomainSourceKind.CRAWL_LINK,
            origin=f"light:{host.hostname}",
            clock=self.ctx.clock,
        )
        return result.domains_new

    def _reschedule(
        self, session: Session, domain: Domain, plan: ScanPlan, o: LightScanOutcome, now: datetime
    ) -> None:
        s = self.ctx.settings.scan
        cycle = interval_for(ScanType.LIGHT, domain.status, on_watchlist=False, s=s)
        if o.status == ScanStatus.OK:
            queue.complete(session, plan, next_scan_at=now + cycle, clock=self.ctx.clock)
        elif o.status == ScanStatus.BLOCKED:
            from datetime import timedelta

            queue.complete(
                session,
                plan,
                next_scan_at=now + timedelta(days=s.blocked_cooldown_days),
                clock=self.ctx.clock,
            )
            plan.last_error = o.stop_reason
        else:
            queue.fail(
                session,
                plan,
                error=o.stop_reason or o.status.value,
                cycle=cycle,
                s=s,
                clock=self.ctx.clock,
            )


async def run_batch(
    factory: sessionmaker[Session],
    scanner: LightScanner,
    *,
    limit: int,
    concurrency: int,
) -> list[LightScanOutcome]:
    """Lease up to `limit` light tasks and scan them concurrently (one session per task)."""
    ctx = scanner.ctx
    with factory() as session:
        plans = queue.lease(
            session,
            ScanType.LIGHT,
            ctx.worker_id,
            limit=limit,
            lease_seconds=ctx.settings.scan.lease_seconds,
            clock=ctx.clock,
        )
        plan_ids = [p.id for p in plans]
        session.commit()
    sem = asyncio.Semaphore(concurrency)
    outcomes: list[LightScanOutcome] = []

    async def one(plan_id: int) -> None:
        async with sem:
            with factory() as session:
                plan = session.execute(select(ScanPlan).where(ScanPlan.id == plan_id)).scalar_one()
                try:
                    outcome = await scanner.scan(session, plan)
                    session.commit()
                    outcomes.append(outcome)
                except Exception as exc:
                    session.rollback()
                    log.error("light scan crashed", plan_id=plan_id, error=repr(exc))
                    with factory() as s2:
                        p2 = s2.get(ScanPlan, plan_id)
                        if p2 is not None:
                            from datetime import timedelta

                            queue.fail(
                                s2,
                                p2,
                                error=repr(exc)[:500],
                                cycle=timedelta(
                                    days=ctx.settings.scan.light_interval_days_candidate
                                ),
                                s=ctx.settings.scan,
                                clock=ctx.clock,
                            )
                            s2.commit()

    await asyncio.gather(*(one(pid) for pid in plan_ids))
    ctx.buffer.flush()
    return outcomes
