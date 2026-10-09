"""Re-detect from stored artefacts without a new scan (FR-DT-12, ADR-0001 §1).

Scanning and detection are separate: the scanners only save artefacts
(`manifest.json`, cleaned HTML pages, network lists), the detector works on
them. When a new ruleset version is released, `redetect_host` rebuilds the
`PageSignals` of the latest light and checkout runs of a store from those
artefacts, runs the current rules and compares the result with the stored
`store_provider` / `store_payment_method` rows. A dry run only reports (and
can be exported in the `import_findings_csv` format); `apply=True` upserts the
detected providers/methods with the new ruleset and writes an audit row. Rows
that are no longer detected are reported as `removed` candidates but never
deleted here: removal keeps going through the differ's confirmations.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import Clock
from payintel.core.errors import StorageError
from payintel.core.models.base import ConfidenceLevel, ProviderRole, RuleTargetType, ScanType
from payintel.core.models.domains import Domain, Host
from payintel.core.models.scans import ScanRun
from payintel.core.models.store import StorePaymentMethod, StoreProvider
from payintel.core.s3 import ObjectStore
from payintel.crawl.light.html import extract
from payintel.detect.engine import PageSignals, match_pages
from payintel.detect.rules import RuleSet
from payintel.detect.scoring import TargetScore, aggregate
from payintel.history.differ import (
    CheckoutObservation,
    SeenMethod,
    SeenProvider,
    apply_checkout_observation,
)
from payintel.history.materialize import ProviderObservation, upsert_providers

CSV_COLUMNS = (
    "domain",
    "entity_type",
    "entity_id",
    "confidence",
    "confidence_score",
    "rule_id",
    "rule_version",
    "active_on_checkout",
)


@dataclass(frozen=True)
class Change:
    entity_type: str  # provider | payment_method
    entity_id: str
    kind: str  # added | removed | confidence
    before: str | None
    after: str | None


@dataclass
class RedetectResult:
    host_id: int
    etld1: str
    ruleset_version: str
    runs: list[str] = field(default_factory=list)
    pages: int = 0
    scores: list[TargetScore] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    applied: bool = False
    note: str | None = None

    @property
    def providers(self) -> list[TargetScore]:
        return [s for s in self.scores if s.target_type == RuleTargetType.PROVIDER]

    @property
    def methods(self) -> list[TargetScore]:
        return [s for s in self.scores if s.target_type == RuleTargetType.PAYMENT_METHOD]


class ArtifactReader:
    def __init__(self, store: ObjectStore, bucket: str) -> None:
        self._store, self._bucket = store, bucket

    def json(self, key: str) -> dict[str, Any] | None:
        try:
            raw = self._store.get_bytes(self._bucket, key)
        except StorageError:
            return None
        doc = json.loads(raw)
        return doc if isinstance(doc, dict) else None

    def html(self, key: str) -> str:
        try:
            raw = self._store.get_bytes(self._bucket, key)
        except StorageError:
            return ""
        try:
            return gzip.decompress(raw).decode("utf-8", "replace")
        except (OSError, EOFError):
            return raw.decode("utf-8", "replace")


def latest_runs(session: Session, host_id: int, *, since: datetime | None = None) -> list[ScanRun]:
    """The newest light and the newest checkout run of the host that left artefacts."""
    out = []
    for scan_type in (ScanType.LIGHT, ScanType.CHECKOUT):
        q = (
            select(ScanRun)
            .where(
                ScanRun.host_id == host_id,
                ScanRun.scan_type == scan_type,
                ScanRun.artifact_prefix.is_not(None),
            )
            .order_by(ScanRun.started_at.desc())
            .limit(1)
        )
        if since is not None:
            q = q.where(ScanRun.started_at >= since)
        run = session.execute(q).scalar_one_or_none()
        if run is not None:
            out.append(run)
    return out


def _cookies_of(headers: dict[str, str]) -> set[str]:
    out: set[str] = set()
    for k, v in headers.items():
        if k.lower() == "set-cookie":
            for part in str(v).split("\n"):
                name = part.split("=", 1)[0].strip()
                if name:
                    out.add(name)
    return out


def signals_of_light(reader: ArtifactReader, manifest: dict[str, Any]) -> list[PageSignals]:
    pages: list[PageSignals] = []
    for entry in manifest.get("pages", []):
        url = str(entry.get("final_url") or entry.get("url") or "")
        html = reader.html(str(entry["html_key"])) if entry.get("html_key") else ""
        headers = {str(k).lower(): str(v) for k, v in (entry.get("headers") or {}).items()}
        if html:
            f = extract(html, url)
            pages.append(
                PageSignals(
                    page_type=str(entry.get("page_type") or "homepage"),
                    url=url,
                    html=html,
                    headers=headers,
                    cookie_names=_cookies_of(headers),
                    script_srcs=f.scripts_src,
                    iframe_srcs=f.iframes_src,
                    form_actions=f.forms_action,
                    network_hosts=set(entry.get("external_hosts") or []) | f.external_hosts(),
                    js_globals=f.js_globals(),
                )
            )
        else:
            pages.append(
                PageSignals(
                    page_type=str(entry.get("page_type") or "homepage"),
                    url=url,
                    headers=headers,
                    cookie_names=_cookies_of(headers),
                    script_srcs=[str(s) for s in entry.get("scripts") or []],
                    iframe_srcs=[str(s) for s in entry.get("iframes") or []],
                    network_hosts=set(entry.get("external_hosts") or []),
                )
            )
    return pages


def signals_of_checkout(reader: ArtifactReader, manifest: dict[str, Any]) -> list[PageSignals]:
    cap = manifest.get("capture") or {}
    url = str(cap.get("url") or manifest.get("checkout_url") or manifest.get("final_url") or "")
    html = reader.html(str(cap["html_key"])) if cap.get("html_key") else ""
    hosts: set[str] = set()
    for row in manifest.get("network") or []:
        h = row.get("host") or urlsplit(str(row.get("url") or "")).hostname
        if h:
            hosts.add(str(h).lower())
    for h in manifest.get("third_party_hosts") or []:
        if h.get("host"):
            hosts.add(str(h["host"]).lower())
    headers = {str(k).lower(): str(v) for k, v in (manifest.get("main_headers") or {}).items()}
    f = extract(html, url) if html else None
    return [
        PageSignals(
            page_type="checkout",
            url=url,
            html=html,
            headers=headers,
            cookie_names=set(manifest.get("cookie_names") or []) | _cookies_of(headers),
            script_srcs=f.scripts_src if f else [],
            iframe_srcs=f.iframes_src if f else [],
            form_actions=f.forms_action if f else [],
            network_hosts=hosts | (f.external_hosts() if f else set()),
            js_globals=set(cap.get("js_globals") or []) | (f.js_globals() if f else set()),
            favicon_sha256=cap.get("favicon_sha256"),
            checkout_labels=[str(x) for x in cap.get("labels") or []],
        )
    ]


def _diff(session: Session, host_id: int, scores: list[TargetScore]) -> list[Change]:
    changes: list[Change] = []
    before_p = {
        r.provider_id: r.confidence.value
        for r in session.execute(
            select(StoreProvider).where(StoreProvider.host_id == host_id)
        ).scalars()
    }
    before_m = {
        r.method_id: r.confidence.value
        for r in session.execute(
            select(StorePaymentMethod).where(StorePaymentMethod.host_id == host_id)
        ).scalars()
    }
    for etype, before, target in (
        ("provider", before_p, RuleTargetType.PROVIDER),
        ("payment_method", before_m, RuleTargetType.PAYMENT_METHOD),
    ):
        after = {s.target_id: s.confidence.value for s in scores if s.target_type == target}
        for eid, conf in sorted(after.items()):
            if eid not in before:
                changes.append(Change(etype, eid, "added", None, conf))
            elif before[eid] != conf:
                changes.append(Change(etype, eid, "confidence", before[eid], conf))
        for eid, conf in sorted(before.items()):
            if eid not in after:
                changes.append(Change(etype, eid, "removed", conf, None))
    return changes


def redetect_host(
    session: Session,
    reader: ArtifactReader,
    ruleset: RuleSet,
    host_id: int,
    *,
    provider_roles: dict[str, ProviderRole],
    apply: bool,
    actor: str,
    clock: Clock,
    since: datetime | None = None,
) -> RedetectResult:
    host = session.get(Host, host_id)
    if host is None:
        raise ValueError(f"host {host_id} not found")
    domain = session.get(Domain, host.domain_id)
    result = RedetectResult(host_id, domain.etld1 if domain else host.hostname, ruleset.version)
    pages: list[PageSignals] = []
    checkout_run: ScanRun | None = None
    for run in latest_runs(session, host_id, since=since):
        manifest = reader.json(f"{run.artifact_prefix}manifest.json")
        if manifest is None:
            continue
        result.runs.append(str(run.id))
        if run.scan_type == ScanType.LIGHT:
            pages.extend(signals_of_light(reader, manifest))
        else:
            checkout_run = run
            pages.extend(signals_of_checkout(reader, manifest))
    result.pages = len(pages)
    if not pages:
        result.note = "no artefacts found for this host"
        return result
    result.scores = aggregate(match_pages(ruleset, pages))
    result.changes = _diff(session, host_id, result.scores)
    if not apply:
        return result
    now = clock.now()
    if checkout_run is not None:
        apply_checkout_observation(
            session,
            host_id,
            CheckoutObservation(
                scan_run_id=checkout_run.id,
                status=checkout_run.status,
                providers=[
                    SeenProvider(
                        s.target_id,
                        provider_roles.get(s.target_id, ProviderRole.GATEWAY),
                        s.confidence,
                        s.score,
                        s.active_on_checkout,
                    )
                    for s in result.providers
                ],
                methods=[
                    SeenMethod(s.target_id, None, s.confidence, s.score) for s in result.methods
                ],
            ),
            now=now,
        )
    else:
        upsert_providers(
            session,
            host_id,
            [
                ProviderObservation(
                    provider_id=s.target_id,
                    role=provider_roles.get(s.target_id, ProviderRole.GATEWAY),
                    confidence=ConfidenceLevel.LOW,
                    score=s.score,
                    active_on_checkout=False,
                )
                for s in result.providers
            ],
            scanned_at=now,
        )
    result.applied = True
    audit.record(
        session,
        actor=actor,
        action="detect.redetect",
        object_type="host",
        object_id=str(host_id),
        after={
            "ruleset_version": ruleset.version,
            "runs": result.runs,
            "changes": [c.__dict__ for c in result.changes][:50],
        },
        clock=clock,
    )
    return result


def candidate_hosts(
    session: Session, *, limit: int, since_days: int | None, now: datetime
) -> list[int]:
    """Hosts with artefacts, newest run first (optionally only runs of the last N days)."""
    q = (
        select(ScanRun.host_id, ScanRun.started_at)
        .where(ScanRun.artifact_prefix.is_not(None))
        .order_by(ScanRun.started_at.desc())
    )
    if since_days is not None:
        q = q.where(ScanRun.started_at >= now - timedelta(days=since_days))
    seen: list[int] = []
    for host_id, _ in session.execute(q.limit(limit * 4)):
        if host_id not in seen:
            seen.append(int(host_id))
        if len(seen) >= limit:
            break
    return seen


def to_csv(results: list[RedetectResult]) -> str:
    """Findings CSV in the `import_findings_csv` format (one row per detected entity)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in results:
        for s in r.scores:
            if s.target_type not in (RuleTargetType.PROVIDER, RuleTargetType.PAYMENT_METHOD):
                continue
            top = max(s.findings, key=lambda f: f.rule.weight)
            w.writerow(
                [
                    r.etld1,
                    s.target_type.value,
                    s.target_id,
                    s.confidence.value,
                    f"{s.score:.3f}",
                    top.rule.rule_id,
                    top.rule.version,
                    "true" if s.active_on_checkout else "false",
                ]
            )
    return buf.getvalue()


def summary_line(r: RedetectResult) -> str:
    kinds = {"added": 0, "removed": 0, "confidence": 0}
    for c in r.changes:
        kinds[c.kind] += 1
    tail = (" applied" if r.applied else "") + (f" ({r.note})" if r.note else "")
    return (
        f"{r.etld1}: runs={len(r.runs)} pages={r.pages} providers={len(r.providers)} "
        f"methods={len(r.methods)} +{kinds['added']} -{kinds['removed']} "
        f"~{kinds['confidence']}{tail}"
    )
