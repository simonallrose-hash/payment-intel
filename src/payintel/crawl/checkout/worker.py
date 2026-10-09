"""Checkout scan worker (4.4): lease → robots → walk → detect → persist.

One `CheckoutScanner` serves many concurrent walks (asyncio, one browser per
process, one isolated context per walk). Each leased `scan_plan` ends in
exactly one `scan_run` whose id is derived from the lease (NFR-R-05) and in:

- `obs_scan` (+ `obs_scan_stop` when the payment step was not reached) in
  ClickHouse, `obs_provider` / `obs_payment_method` / `obs_tech` for every
  finding of the payment-step rules and `obs_checkout_host` for every
  third-party host the checkout page talked to (FR-DT-11);
- artefacts under `checkout/<etld1>/<run_id>/` (FR-CW-08): the manifest
  with the walk journal, step timings, request log and findings; the
  sanitised payment-step DOM and payment block; the screenshot; the HAR
  without cookies, authorisation headers or request bodies; and, for a
  stop, `stop.jpg` + `stop.html.gz` (FR-CW-13);
- the current state in PostgreSQL through the differ (FR-HI-02 … FR-HI-04)
  and the profile's checkout fields (`checkout_status`, `coverage`,
  `checkout_country`, `acquirer_hidden`, `last_checkout_scan_at`);
- the system account created or used during the walk (FR-CW-12);
- the next scan date (FR-SC-03; `blocked` → 30-day cool-down, FR-CW-06).

Safety flags (FR-ADM-05) are read once per walk, at lease time; robots.txt is
read before the browser is opened and every explicit navigation of the walk
is checked against it (FR-CW-02, LR-03).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.flags import FlagService
from payintel.core.logging import get_logger, log_context
from payintel.core.models.alerts import WatchlistItem
from payintel.core.models.base import (
    Coverage,
    ProviderRole,
    RuleTargetType,
    ScanStatus,
    ScanType,
    StoreAccountStatus,
)
from payintel.core.models.domains import Domain, Host
from payintel.core.models.scans import ScanPlan, ScanRun
from payintel.core.models.store import StoreCheckoutHost, StoreProfile
from payintel.core.reference_loader import ReferenceData
from payintel.core.settings import Settings
from payintel.crawl.checkout import geo as geo_mod
from payintel.crawl.checkout.accounts import AccountManager, Credentials
from payintel.crawl.checkout.capture import NetworkEntry, network_rows
from payintel.crawl.checkout.identities import IdentityProvider
from payintel.crawl.checkout.stops import StopContext, artifact_keys, normalise_stop, stop_row
from payintel.crawl.checkout.types import Stop, WalkFlags, WalkStep
from payintel.crawl.checkout.walker import CheckoutWalker, WalkInput, WalkResult
from payintel.crawl.light.artifacts import ArtifactWriter, sha256_hex
from payintel.crawl.light.fetcher import Fetcher
from payintel.crawl.light.html import extract
from payintel.crawl.light.robots import RobotsRules, agent_token, parse_robots
from payintel.crawl.sanitize import sanitize_headers
from payintel.detect.country import CountryDetector, CountryResult
from payintel.detect.engine import Finding, PageSignals, match_page
from payintel.detect.hosts import HostCategorizer
from payintel.detect.rules import RuleSet
from payintel.detect.scoring import TargetScore, aggregate, best_platform
from payintel.history.differ import (
    CheckoutObservation,
    DiffResult,
    SeenHost,
    SeenMethod,
    SeenProvider,
    apply_checkout_observation,
)
from payintel.history.materialize import ProfileUpdate, upsert_profile
from payintel.history.writer import ObservationBuffer
from payintel.scheduler import queue
from payintel.scheduler.planner import interval_for

log = get_logger("payintel.crawl.checkout")

ROBOTS_MAX_BYTES = 512 * 1024
HAR_SCRUBBED_HEADERS = frozenset(
    {"cookie", "set-cookie", "authorization", "proxy-authorization", "x-csrf-token", "x-xsrf-token"}
)
# Statuses that are not "the walk ran" (FR-SC-05 failures / FR-CW-06 blocks).
FAILED_STATUSES = frozenset({ScanStatus.TIMEOUT, ScanStatus.ERROR})
RobotsFetch = Callable[[str], Awaitable[RobotsRules]]


@dataclass(frozen=True)
class ThirdPartyHost:
    """One third-party host the checkout page talked to (FR-DT-11)."""

    host: str
    etld1: str
    category: str
    provider_id: str | None
    request_count: int
    resource_type: str
    initiator: str


@dataclass
class CheckoutOutcome:
    scan_run_id: uuid.UUID
    host_id: int
    etld1: str
    status: ScanStatus
    coverage: Coverage
    stop: Stop | None
    walk: WalkResult | None
    scores: list[TargetScore]
    platform: TargetScore | None
    country: CountryResult | None
    hosts: list[ThirdPartyHost]
    acquirer_hidden: bool
    identity_country: str
    artifact_prefix: str
    duration_ms: int
    flags: WalkFlags
    credentials: Credentials | None = None
    findings: list[Finding] = field(default_factory=list)
    diff: DiffResult | None = None
    robots_fetched: bool = True
    geo: dict[str, str | None] = field(default_factory=dict)  # FR-CW-11 (proxy redacted)

    @property
    def providers(self) -> list[TargetScore]:
        return [s for s in self.scores if s.target_type == RuleTargetType.PROVIDER]

    @property
    def methods(self) -> list[TargetScore]:
        return [s for s in self.scores if s.target_type == RuleTargetType.PAYMENT_METHOD]

    @property
    def reached_payment(self) -> bool:
        return self.status == ScanStatus.REACHED_PAYMENT_STEP

    @property
    def adapter(self) -> str:
        return self.walk.adapter if self.walk is not None else "none"


@dataclass
class CheckoutContext:
    settings: Settings
    reference: ReferenceData
    ruleset: RuleSet
    provider_roles: dict[str, ProviderRole]
    hosts: HostCategorizer
    country: CountryDetector
    fetcher: Fetcher
    buffer: ObservationBuffer
    writer: ArtifactWriter
    walker: CheckoutWalker
    identities: IdentityProvider
    accounts: AccountManager | None
    clock: Clock = SYSTEM_CLOCK
    worker_id: str = "worker-checkout-0"
    base_scheme: str = "https"
    robots_fetch: RobotsFetch | None = None

    @property
    def agent_token(self) -> str:
        return agent_token(self.settings.identity.user_agent)


@dataclass(frozen=True)
class _Lease:
    """What one walk needs from PostgreSQL, loaded before the browser phase (no open session)."""

    plan_id: int
    run_id: uuid.UUID
    host_id: int
    hostname: str
    hosting_country: str | None
    domain_id: int
    etld1: str
    flags: WalkFlags
    credentials: Credentials | None
    profile_country: str | None
    profile_platform: str | None
    on_watchlist: bool


class CheckoutScanner:
    """One instance per worker process; `scan` and `scan_plan` are safe to run concurrently."""

    def __init__(self, ctx: CheckoutContext) -> None:
        self.ctx = ctx
        self._etld1_locks: dict[str, asyncio.Lock] = {}

    # --- lease-time reads ----------------------------------------------------------
    def _load(self, session: Session, plan: ScanPlan) -> _Lease:
        host = session.get(Host, plan.host_id)
        if host is None:
            raise LookupError(f"plan {plan.id} without host")
        domain = session.get(Domain, host.domain_id)
        if domain is None:
            raise LookupError(f"host {host.id} without domain")
        flags = FlagService(session, self.ctx.settings.flags, self.ctx.clock)
        registration_allowed = flags.is_enabled("allow_account_registration")
        if self.ctx.accounts is None and registration_allowed:
            # FR-CW-12: a registered account must be stored encrypted; without a key, no account.
            registration_allowed = False
        walk_flags = WalkFlags(
            allow_shipping_step_fill=flags.is_enabled("allow_shipping_step_fill"),
            allow_account_registration=registration_allowed,
            allow_payment_field_fill=flags.is_enabled("allow_payment_field_fill"),
        )
        credentials = (
            self.ctx.accounts.get(session, host.id) if self.ctx.accounts is not None else None
        )
        profile = session.get(StoreProfile, host.id)
        on_watchlist = (
            session.scalar(
                select(WatchlistItem.id).where(WatchlistItem.domain == domain.etld1).limit(1)
            )
            is not None
        )
        return _Lease(
            plan_id=plan.id,
            run_id=queue.run_id_for(plan),
            host_id=host.id,
            hostname=host.hostname,
            hosting_country=host.hosting_country,
            domain_id=domain.id,
            etld1=domain.etld1,
            flags=walk_flags,
            credentials=credentials,
            profile_country=profile.country if profile else None,
            profile_platform=profile.platform_id if profile else None,
            on_watchlist=on_watchlist,
        )

    # --- entry points -------------------------------------------------------------------
    async def scan(self, session: Session, plan: ScanPlan) -> CheckoutOutcome:
        """Scan a leased plan inside one caller-owned session (tests, single-task use)."""
        lease = self._load(session, plan)
        started = self.ctx.clock.now()
        with log_context(scan_run_id=str(lease.run_id)):
            outcome = await self._browser_phase(lease)
            self._persist(session, plan, lease, outcome, started)
        return outcome

    async def scan_plan(self, factory: sessionmaker[Session], plan_id: int) -> CheckoutOutcome:
        """Scan without holding a database connection across the browser phase."""
        with factory() as session:
            plan = session.get(ScanPlan, plan_id)
            if plan is None:
                raise LookupError(f"plan {plan_id} vanished")
            lease = self._load(session, plan)
        started = self.ctx.clock.now()
        with log_context(scan_run_id=str(lease.run_id)):
            outcome = await self._browser_phase(lease)

            def persist() -> None:
                with factory() as s2:
                    plan2 = s2.get(ScanPlan, plan_id)
                    if plan2 is None:
                        raise LookupError(f"plan {plan_id} vanished during the scan")
                    self._persist(s2, plan2, lease, outcome, started)
                    s2.commit()

            await asyncio.to_thread(persist)
        return outcome

    # --- browser phase ---------------------------------------------------------------------
    async def _robots(self, base: str) -> RobotsRules:
        if self.ctx.robots_fetch is not None:
            return await self.ctx.robots_fetch(base)
        r = await self.ctx.fetcher.fetch(urljoin(base, "/robots.txt"), max_bytes=ROBOTS_MAX_BYTES)
        body = r.text() if r.error is None and r.status == 200 else None
        return parse_robots(body, None if r.error else r.status, agent_token=self.ctx.agent_token)

    def _lock_for(self, etld1: str) -> asyncio.Lock:
        # FR-SC-06: at most `max_browser_sessions_per_etld1` (=1) walks per eTLD+1 per worker.
        lock = self._etld1_locks.get(etld1)
        if lock is None:
            lock = self._etld1_locks[etld1] = asyncio.Lock()
        return lock

    async def _browser_phase(self, lease: _Lease) -> CheckoutOutcome:
        t0 = asyncio.get_running_loop().time()
        prefix = f"checkout/{lease.etld1}/{lease.run_id}/"
        base = f"{self.ctx.base_scheme}://{lease.hostname}/"
        identity = self.ctx.identities.for_country(
            lease.profile_country, email_token=f"h{lease.host_id}"
        )
        # FR-CW-11: decided once, before the walk; never revisited after a block (LR-03)
        geo = geo_mod.choose(
            identity.country,
            egress_country=self.ctx.settings.checkout.egress_country,
            proxies=self.ctx.settings.checkout.geo_proxies,
        )
        robots = await self._robots(base)
        if not robots.allows(base):
            stop = Stop(
                WalkStep.NAVIGATION,
                "robots_disallowed",
                "robots.txt disallows the homepage" if robots.fetched else "robots.txt unavailable",
                page_url=base,
            )
            outcome = self._outcome(lease, prefix, identity.country, stop=stop, walk=None)
            outcome.geo = geo.as_log()
            outcome.robots_fetched = robots.fetched
            outcome.duration_ms = int((asyncio.get_running_loop().time() - t0) * 1000)
            return outcome

        etld1 = lease.etld1

        def url_check(url: str) -> str | None:
            h = (urlsplit(url).hostname or "").lower()
            if not h:
                return "navigation_error"
            if h != etld1 and not h.endswith("." + etld1):
                return "navigation_error"  # the walk never leaves the shop's eTLD+1
            if not robots.allows(url):
                return "robots_disallowed"
            return None

        inp = WalkInput(
            url=base,
            etld1=etld1,
            identity=identity,
            flags=lease.flags,
            url_check=url_check,
            platform_id=lease.profile_platform,
            credentials=lease.credentials,
            platform_detect=self._platform_from_homepage,
            proxy=geo.proxy.as_playwright() if geo.proxy else None,
            geo=geo.as_log(),
        )
        async with self._lock_for(etld1):
            walk = await self.ctx.walker.walk(inp)
        normalised: Stop | None = None
        if walk.stop is not None:
            normalised = normalise_stop(
                walk.stop,
                self.ctx.reference.stop_reasons,
                detail_max_chars=self.ctx.settings.checkout.stop_detail_max_chars,
            )
        outcome = self._outcome(lease, prefix, identity.country, stop=normalised, walk=walk)
        outcome.geo = geo.as_log()
        outcome.duration_ms = int((asyncio.get_running_loop().time() - t0) * 1000)
        return outcome

    def _platform_from_homepage(self, html: str, url: str) -> str | None:
        """Adapter for a shop without a light-scan profile: platform rules on the homepage."""
        features = extract(html, url)
        signals = PageSignals(
            page_type="homepage",
            url=url,
            html=html,
            script_srcs=features.scripts_src,
            iframe_srcs=features.iframes_src,
            form_actions=features.forms_action,
            network_hosts=features.external_hosts(),
            js_globals=features.js_globals(),
        )
        platform = best_platform(aggregate(match_page(self.ctx.ruleset, signals)))
        return platform.target_id if platform else None

    def _outcome(
        self,
        lease: _Lease,
        prefix: str,
        identity_country: str,
        *,
        stop: Stop | None,
        walk: WalkResult | None,
    ) -> CheckoutOutcome:
        findings: list[Finding] = []
        country: CountryResult | None = None
        hosts: list[ThirdPartyHost] = []
        if walk is not None and walk.capture is not None:
            cap = walk.capture
            findings = match_page(self.ctx.ruleset, cap.signals(walk.recorder.hosts()))
            if cap.features is not None:
                country = self.ctx.country.detect(
                    cap.features,
                    etld1=lease.etld1,
                    raw_text=cap.body_text,
                    hosting_country=lease.hosting_country,
                )
        scores = aggregate(findings)
        platform = best_platform(scores)
        status = walk.status if walk is not None else (stop.status if stop else ScanStatus.ERROR)
        coverage = (
            walk.coverage if walk is not None else (stop.coverage if stop else Coverage.HOMEPAGE)
        )
        if walk is not None and status == ScanStatus.REACHED_PAYMENT_STEP:
            hosts = self._third_party_hosts(walk, lease.etld1)
        providers = [s for s in scores if s.target_type == RuleTargetType.PROVIDER]
        return CheckoutOutcome(
            scan_run_id=lease.run_id,
            host_id=lease.host_id,
            etld1=lease.etld1,
            status=status,
            coverage=coverage,
            stop=stop,
            walk=walk,
            scores=scores,
            platform=platform,
            country=country,
            hosts=hosts,
            acquirer_hidden=self._acquirer_hidden(walk, providers),
            identity_country=identity_country,
            artifact_prefix=prefix,
            duration_ms=0,
            flags=lease.flags,
            credentials=lease.credentials,
            findings=findings,
        )

    def _acquirer_hidden(self, walk: WalkResult | None, providers: list[TargetScore]) -> bool:
        """ADR-0015: the payment step is served by an orchestrator or a tokenizer and no
        gateway/acquirer is visible among the active providers → the acquirer is hidden."""
        if walk is None or walk.stop is not None:
            return False
        active = [p for p in providers if p.active_on_checkout]
        roles = {self.ctx.provider_roles.get(p.target_id, ProviderRole.GATEWAY) for p in active}
        fronted = bool(walk.tokenizer) or ProviderRole.ORCHESTRATOR in roles
        return fronted and ProviderRole.GATEWAY not in roles

    def _third_party_hosts(self, walk: WalkResult, etld1: str) -> list[ThirdPartyHost]:
        """Third-party hosts contacted from the checkout step on (FR-DT-11): every request
        the browser made after the walk entered the checkout, in any frame."""
        if walk.checkout_entry_index < 0:
            return []
        grouped: dict[str, list[NetworkEntry]] = {}
        for e in walk.recorder.entries[walk.checkout_entry_index :]:
            if not e.host or e.host == etld1 or e.host.endswith("." + etld1):
                continue
            grouped.setdefault(e.host, []).append(e)
        out: list[ThirdPartyHost] = []
        for host, picked in sorted(grouped.items()):
            cat = self.ctx.hosts.classify(host)
            types: dict[str, int] = {}
            inits: dict[str, int] = {}
            for e in picked:
                types[e.resource_type] = types.get(e.resource_type, 0) + 1
                if e.initiator:
                    inits[e.initiator] = inits.get(e.initiator, 0) + 1
            out.append(
                ThirdPartyHost(
                    host=host,
                    etld1=cat.etld1,
                    category=cat.category,
                    provider_id=cat.provider_id,
                    request_count=len(picked),
                    resource_type=max(types, key=lambda k: types[k]) if types else "",
                    initiator=max(inits, key=lambda k: inits[k]) if inits else "",
                )
            )
        return out

    # --- persistence --------------------------------------------------------------------------
    def _persist(
        self, session: Session, plan: ScanPlan, lease: _Lease, o: CheckoutOutcome, started: datetime
    ) -> None:
        now = self.ctx.clock.now()
        host = session.get(Host, lease.host_id)
        domain = session.get(Domain, lease.domain_id)
        if host is None or domain is None:
            raise LookupError(f"host {lease.host_id} vanished during the scan")
        if session.get(ScanRun, o.scan_run_id) is None:
            session.add(
                ScanRun(
                    id=o.scan_run_id,
                    host_id=host.id,
                    scan_type=ScanType.CHECKOUT,
                    started_at=started,
                    finished_at=now,
                    status=o.status,
                    coverage=o.coverage,
                    stop_step=o.stop.step.value if o.stop else None,
                    stop_reason=o.stop.reason if o.stop else None,
                    checkout_country=o.identity_country,
                    used_account=bool(o.walk and o.walk.used_account),
                    worker_id=self.ctx.worker_id,
                    ruleset_version=self.ctx.ruleset.version,
                    artifact_prefix=o.artifact_prefix,
                )
            )
            session.flush()
        self._write_artifacts(o)
        known_hosts = set(
            session.execute(
                select(StoreCheckoutHost.third_party_etld1).where(
                    StoreCheckoutHost.host_id == host.id
                )
            ).scalars()
        )
        self._write_observations(o, now, known_hosts)
        # the differ compares against the profile as it was before this scan
        o.diff = apply_checkout_observation(session, host.id, self._observation(o), now=now)
        self._apply_profile(session, host, domain, o, now)
        self._apply_accounts(session, host, o, now)
        self._reschedule(session, domain, plan, lease, o, now)
        log.info(
            "checkout scan finished",
            host=host.hostname,
            status=o.status.value,
            coverage=o.coverage.value,
            stop=f"{o.stop.step.value}/{o.stop.reason}" if o.stop else None,
            adapter=o.adapter,
            platform=o.platform.target_id if o.platform else None,
            providers=[p.target_id for p in o.providers],
            methods=[m.target_id for m in o.methods],
            hosts=len(o.hosts),
            events=o.diff.by_type() if o.diff else {},
            duration_ms=o.duration_ms,
        )

    # --- artefacts (FR-CW-08) ------------------------------------------------------------------
    def _write_artifacts(self, o: CheckoutOutcome) -> None:
        w = self.ctx.writer
        prefix = o.artifact_prefix
        manifest: dict[str, Any] = {
            "scan_run_id": str(o.scan_run_id),
            "scan_type": "checkout",
            "etld1": o.etld1,
            "status": o.status.value,
            "coverage": o.coverage.value,
            "identity_country": o.identity_country,
            "geo": o.geo,
            "flags": o.flags.__dict__,
            "ruleset_version": self.ctx.ruleset.version,
            "scanner_version": self.ctx.settings.scanner_version,
            "acquirer_hidden": o.acquirer_hidden,
            "robots_fetched": o.robots_fetched,
            "findings": [
                {
                    "rule_id": f.rule.rule_id,
                    "version": f.rule.version,
                    "value": f.signal_value,
                    "page_type": f.page_type,
                }
                for f in o.findings
            ],
            "third_party_hosts": [h.__dict__ for h in o.hosts],
            "stop": (
                {
                    "step": o.stop.step.value,
                    "reason": o.stop.reason,
                    "detail": o.stop.detail,
                    "page_url": o.stop.page_url,
                    "element_selector": o.stop.element_selector,
                    "element_text": o.stop.element_text,
                    "http_status": o.stop.http_status,
                }
                if o.stop
                else None
            ),
        }
        walk = o.walk
        if walk is not None:
            rec = walk.recorder
            headers, _ = sanitize_headers(rec.main_headers)
            manifest.update(
                {
                    "adapter": walk.adapter,
                    "duration_ms": walk.duration_ms,
                    "furthest_step": walk.furthest.value,
                    "final_url": walk.final_url,
                    "product_url": walk.product_url,
                    "checkout_url": walk.checkout_url,
                    "steps": [{"name": s.name, "duration_ms": s.duration_ms} for s in walk.steps],
                    "journal": walk.journal.as_list(),
                    "network": network_rows(rec),
                    "redirects": rec.redirects,
                    "main_status": rec.main_status,
                    "main_headers": headers,
                    "tls": rec.tls.__dict__ if rec.tls else None,
                    "dom_blocked": walk.dom_blocked,
                    "blocked_posts": walk.blocked_posts,
                    "refused_hosts": walk.refused_hosts,
                    "cookie_names": walk.cookie_names,
                    "tokenizer": walk.tokenizer,
                    "methods_revealed": walk.methods_revealed,
                    "payment_fill": walk.payment_fill.__dict__ if walk.payment_fill else None,
                    "used_account": walk.used_account,
                    "registration": (
                        {
                            "status": walk.registration.status.value,
                            "outcome": walk.registration.outcome,
                        }
                        if walk.registration
                        else None
                    ),
                }
            )
            cap = walk.capture
            if cap is not None:
                entry: dict[str, Any] = {
                    "url": cap.url,
                    "page_type": cap.page_type,
                    "title": cap.title,
                    "labels": cap.labels,
                    "payment_block_selector": cap.payment_block_selector,
                    "favicon_url": cap.favicon_url,
                    "favicon_sha256": cap.favicon_sha256,
                    "js_globals": sorted(cap.js_globals),
                    "redacted": {"emails": cap.sanitize.emails, "phones": cap.sanitize.phones},
                    "currency_text_sample": cap.currency_text_sample,
                }
                if cap.clean_html:
                    key = f"{prefix}payment_step.html.gz"
                    w.put_html(key, cap.clean_html)
                    entry["html_key"] = key
                    entry["html_sha256"] = sha256_hex(cap.clean_html.encode("utf-8"))
                if cap.payment_block_html:
                    key = f"{prefix}payment_block.html.gz"
                    w.put_html(key, cap.payment_block_html)
                    entry["payment_block_key"] = key
                if cap.screenshot_jpeg:
                    key = f"{prefix}screenshot.jpg"
                    w.put_bytes(key, cap.screenshot_jpeg, content_type="image/jpeg")
                    entry["screenshot_key"] = key
                manifest["capture"] = entry
            if walk.har:
                scrubbed = scrub_har(walk.har)
                if scrubbed is not None:
                    key = f"{prefix}har.json.gz"
                    w.put_bytes(key, gzip.compress(scrubbed), content_type="application/gzip")
                    manifest["har_key"] = key
            if o.stop is not None:
                shot_key, dom_key = artifact_keys(prefix)
                if walk.screenshot:
                    w.put_bytes(shot_key, walk.screenshot, content_type="image/jpeg")
                    manifest["stop_screenshot_key"] = shot_key
                if walk.dom:
                    w.put_html(dom_key, walk.dom)
                    manifest["stop_dom_key"] = dom_key
        w.put_json(f"{prefix}manifest.json", manifest)

    # --- observations (5.2) ---------------------------------------------------------------------
    def _write_observations(self, o: CheckoutOutcome, now: datetime, known_hosts: set[str]) -> None:
        buf = self.ctx.buffer
        platform_id = o.platform.target_id if o.platform else ""
        country = o.country.country if o.country and o.country.country else ""
        evidence = f"{o.artifact_prefix}manifest.json"
        walk = o.walk
        buf.add(
            "obs_scan",
            {
                "scan_date": now.date(),
                "scan_ts": now,
                "scan_run_id": o.scan_run_id,
                "host_id": o.host_id,
                "etld1": o.etld1,
                "scan_type": "checkout",
                "status": o.status.value,
                "coverage": o.coverage.value,
                "duration_ms": o.duration_ms,
                "blocked_by": walk.blocked_by if walk is not None else "",
                "platform_id": platform_id,
                "adapter": o.adapter,
                "scanner_version": self.ctx.settings.scanner_version,
                "ruleset_version": self.ctx.ruleset.version,
            },
        )
        if o.stop is not None:
            shot_key, _dom_key = artifact_keys(o.artifact_prefix)
            buf.add(
                "obs_scan_stop",
                stop_row(
                    o.stop,
                    StopContext(
                        scan_run_id=o.scan_run_id,
                        host_id=o.host_id,
                        etld1=o.etld1,
                        platform_id=platform_id,
                        adapter=o.adapter,
                        scanner_version=self.ctx.settings.scanner_version,
                        ruleset_version=self.ctx.ruleset.version,
                        artifact_prefix=o.artifact_prefix,
                    ),
                    steps=walk.steps if walk is not None else [],
                    now=now,
                    artifact_key=shot_key if walk is not None and walk.screenshot else "",
                ),
            )
        detected = {p.target_id for p in o.providers}
        default_provider = {
            m["id"]: m.get("default_provider_id") for m in self.ctx.reference.payment_methods
        }
        for sc in o.scores:
            if sc.target_type in {RuleTargetType.PLATFORM, RuleTargetType.TECH}:
                for f in sc.findings:
                    buf.add(
                        "obs_tech",
                        {
                            "scan_date": now.date(),
                            "scan_ts": now,
                            "host_id": o.host_id,
                            "tech_id": sc.target_id,
                            "version": "",
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
                            "host_id": o.host_id,
                            "etld1": o.etld1,
                            "provider_id": sc.target_id,
                            "role": role.value,
                            "signal_type": f.rule.signal_type.value,
                            "signal_value": f.signal_value,
                            "page_type": f.page_type,
                            "page_url": f.page_url,
                            "rule_id": f.rule.rule_id,
                            "rule_version": f.rule.version,
                            "confidence": sc.confidence.value,
                            "confidence_score": sc.score,
                            "active_on_checkout": int(sc.active_on_checkout),
                            "evidence_key": evidence,
                            "scan_run_id": o.scan_run_id,
                            "country": country,
                            "platform_id": platform_id,
                            "vertical_id": "",
                        },
                    )
            elif sc.target_type == RuleTargetType.PAYMENT_METHOD:
                dp = default_provider.get(sc.target_id)
                for f in sc.findings:
                    buf.add(
                        "obs_payment_method",
                        {
                            "scan_date": now.date(),
                            "scan_ts": now,
                            "host_id": o.host_id,
                            "etld1": o.etld1,
                            "method_id": sc.target_id,
                            "provider_id": dp if dp in detected else "",
                            "signal_type": f.rule.signal_type.value,
                            "signal_value": f.signal_value,
                            "page_type": f.page_type,
                            "page_url": f.page_url,
                            "rule_id": f.rule.rule_id,
                            "rule_version": f.rule.version,
                            "confidence": sc.confidence.value,
                            "confidence_score": sc.score,
                            "evidence_key": evidence,
                            "scan_run_id": o.scan_run_id,
                            "country": country,
                            "platform_id": platform_id,
                            "vertical_id": "",
                        },
                    )
        for h in o.hosts:
            buf.add(
                "obs_checkout_host",
                {
                    "scan_date": now.date(),
                    "scan_ts": now,
                    "host_id": o.host_id,
                    "third_party_host": h.host,
                    "third_party_etld1": h.etld1,
                    "category": h.category,
                    "resource_type": h.resource_type,
                    "initiator": h.initiator,
                    "request_count": h.request_count,
                    "first_seen": int(h.etld1 not in known_hosts),
                    "scan_run_id": o.scan_run_id,
                },
            )

    # --- current state (FR-HI-02) --------------------------------------------------------------
    def _observation(self, o: CheckoutOutcome) -> CheckoutObservation:
        detected = {p.target_id for p in o.providers}
        default_provider = {
            m["id"]: m.get("default_provider_id") for m in self.ctx.reference.payment_methods
        }
        by_etld1: dict[str, SeenHost] = {}
        for h in o.hosts:
            prev = by_etld1.get(h.etld1)
            by_etld1[h.etld1] = SeenHost(
                etld1=h.etld1,
                category=h.category if prev is None or prev.category == "other" else prev.category,
                provider_id=h.provider_id or (prev.provider_id if prev else None),
                request_count=h.request_count + (prev.request_count if prev else 0),
            )
        return CheckoutObservation(
            scan_run_id=o.scan_run_id,
            status=o.status,
            providers=[
                SeenProvider(
                    provider_id=p.target_id,
                    role=self.ctx.provider_roles.get(p.target_id, ProviderRole.GATEWAY),
                    confidence=p.confidence,
                    score=p.score,
                    active_on_checkout=p.active_on_checkout,
                )
                for p in o.providers
            ],
            methods=[
                SeenMethod(
                    method_id=m.target_id,
                    provider_id=(
                        default_provider.get(m.target_id)
                        if default_provider.get(m.target_id) in detected
                        else None
                    ),
                    confidence=m.confidence,
                    score=m.score,
                )
                for m in o.methods
            ],
            hosts=list(by_etld1.values()),
            platform_id=o.platform.target_id if o.platform else None,
            platform_confidence=o.platform.confidence if o.platform else None,
        )

    def _apply_profile(
        self, session: Session, host: Host, domain: Domain, o: CheckoutOutcome, now: datetime
    ) -> None:
        profile = upsert_profile(
            session,
            host.id,
            ProfileUpdate(
                platform_id=o.platform.target_id if o.platform else None,
                platform_confidence=o.platform.confidence if o.platform else None,
                platform_version=None,
                country=o.country.country if o.country else None,
                country_confidence=o.country.confidence if o.country else None,
                currency=o.country.currency if o.country else None,
                traffic_rank=domain.traffic_rank,
            ),
            scanned_at=now,
            scan_type=ScanType.CHECKOUT,
        )
        profile.checkout_status = o.status
        profile.checkout_country = o.identity_country
        if o.status not in FAILED_STATUSES and o.status != ScanStatus.BLOCKED:
            # a failed run says nothing about how far the shop can be walked
            profile.coverage = o.coverage
            profile.acquirer_hidden = o.acquirer_hidden if o.reached_payment else False
        session.flush()

    def _apply_accounts(
        self, session: Session, host: Host, o: CheckoutOutcome, now: datetime
    ) -> None:
        """FR-CW-12: persist the system account the walk created or used."""
        mgr = self.ctx.accounts
        walk = o.walk
        if mgr is None or walk is None:
            return
        reg = walk.registration
        if reg is not None:
            if o.credentials is not None:
                mgr.touch(session, o.credentials.account_id, status=reg.status, now=now)
            else:
                mgr.create(
                    session,
                    host_id=host.id,
                    scan_run_id=o.scan_run_id,
                    status=reg.status,
                    password=reg.password,
                    now=now,
                )
            return
        if o.credentials is None:
            return
        if walk.used_account:
            mgr.touch(session, o.credentials.account_id, status=StoreAccountStatus.ACTIVE, now=now)
        elif o.stop is not None and o.stop.reason == "login_failed":
            mgr.touch(session, o.credentials.account_id, status=StoreAccountStatus.FAILED, now=now)

    def _reschedule(
        self,
        session: Session,
        domain: Domain,
        plan: ScanPlan,
        lease: _Lease,
        o: CheckoutOutcome,
        now: datetime,
    ) -> None:
        s = self.ctx.settings.scan
        cycle = interval_for(ScanType.CHECKOUT, domain.status, on_watchlist=lease.on_watchlist, s=s)
        if o.status == ScanStatus.BLOCKED:
            queue.complete(
                session,
                plan,
                next_scan_at=now + timedelta(days=s.blocked_cooldown_days),
                clock=self.ctx.clock,
            )
            plan.last_error = f"{o.stop.reason}: {o.stop.detail[:200]}" if o.stop else "blocked"
        elif o.status in FAILED_STATUSES:
            queue.fail(
                session,
                plan,
                error=f"{o.stop.reason}: {o.stop.detail[:200]}" if o.stop else o.status.value,
                cycle=cycle,
                s=s,
                clock=self.ctx.clock,
                scan_run_id=o.scan_run_id,
            )
        else:
            queue.complete(session, plan, next_scan_at=now + cycle, clock=self.ctx.clock)
            plan.last_error = None


def scrub_har(raw: bytes) -> bytes | None:
    """Drop cookies, authorisation headers and request bodies from a HAR (FR-CW-08, NFR-S-07)."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    entries = doc.get("log", {}).get("entries", [])
    for entry in entries:
        for side in ("request", "response"):
            part = entry.get(side)
            if not isinstance(part, dict):
                continue
            part["cookies"] = []
            part["headers"] = [
                h
                for h in part.get("headers", [])
                if str(h.get("name", "")).lower() not in HAR_SCRUBBED_HEADERS
            ]
            if side == "request":
                part.pop("postData", None)
            else:
                content = part.get("content")
                if isinstance(content, dict):
                    content.pop("text", None)
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --- batch runners --------------------------------------------------------------------------------


def _fail_plan(
    factory: sessionmaker[Session], ctx: CheckoutContext, plan_id: int, exc: BaseException
) -> None:
    with factory() as s2:
        p2 = s2.get(ScanPlan, plan_id)
        if p2 is not None:
            queue.fail(
                s2,
                p2,
                error=repr(exc)[:500],
                cycle=timedelta(days=ctx.settings.scan.checkout_interval_days),
                s=ctx.settings.scan,
                clock=ctx.clock,
            )
            s2.commit()


def _lease(factory: sessionmaker[Session], ctx: CheckoutContext, n: int) -> list[int]:
    with factory() as session:
        plans = queue.lease(
            session,
            ScanType.CHECKOUT,
            ctx.worker_id,
            limit=n,
            lease_seconds=ctx.settings.scan.lease_seconds,
            clock=ctx.clock,
        )
        ids = [p.id for p in plans]
        session.commit()
    return ids


async def run_batch(
    factory: sessionmaker[Session],
    scanner: CheckoutScanner,
    *,
    limit: int,
    concurrency: int,
) -> list[CheckoutOutcome]:
    """Lease up to `limit` checkout tasks and walk them concurrently (one browser context each)."""
    ctx = scanner.ctx
    plan_ids = await asyncio.to_thread(_lease, factory, ctx, limit)
    sem = asyncio.Semaphore(max(1, concurrency))
    outcomes: list[CheckoutOutcome] = []

    async def one(plan_id: int) -> None:
        async with sem:
            try:
                outcomes.append(await scanner.scan_plan(factory, plan_id))
            except Exception as exc:
                log.error("checkout scan crashed", plan_id=plan_id, error=repr(exc))
                await asyncio.to_thread(_fail_plan, factory, ctx, plan_id, exc)

    await asyncio.gather(*(one(pid) for pid in plan_ids))
    ctx.buffer.flush()
    return outcomes


async def run_pipeline(
    factory: sessionmaker[Session],
    scanner: CheckoutScanner,
    *,
    concurrency: int,
    poll_seconds: float = 5.0,
    stop_when_empty: bool = False,
    on_outcome: Callable[[CheckoutOutcome], None] | None = None,
) -> int:
    """Keep `concurrency` walks in flight; lease more as slots free up."""
    ctx = scanner.ctx
    inflight: set[asyncio.Task[None]] = set()
    done_count = 0
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=max(4, concurrency), thread_name_prefix="persist")
    )

    async def one(plan_id: int) -> None:
        nonlocal done_count
        try:
            outcome = await scanner.scan_plan(factory, plan_id)
            done_count += 1
            if on_outcome is not None:
                on_outcome(outcome)
        except Exception as exc:
            log.error("checkout scan crashed", plan_id=plan_id, error=repr(exc))
            await asyncio.to_thread(_fail_plan, factory, ctx, plan_id, exc)

    try:
        while True:
            free = concurrency - len(inflight)
            if free > 0:
                ids = await asyncio.to_thread(_lease, factory, ctx, free)
                for pid in ids:
                    task = asyncio.create_task(one(pid))
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
                if not ids and not inflight:
                    if stop_when_empty:
                        break
                    await asyncio.sleep(poll_seconds)
                    continue
            if inflight:
                await asyncio.wait(inflight, return_when=asyncio.FIRST_COMPLETED, timeout=1.0)
            else:
                await asyncio.sleep(poll_seconds)
    finally:
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        ctx.buffer.flush()
    return done_count
