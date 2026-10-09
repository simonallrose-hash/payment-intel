"""Sanctions screening through the OpenSanctions matching API (FR-KYC-03, LR-14).

The organisation (as a `Company` with name, country and registration number)
and every beneficial owner (as a `Person`) are sent as query-by-example to
`POST {api_base}/match/{dataset}`; the service scores candidates from the
OFAC, EU, UK, UN and other lists bundled in the `default` collection.
Candidates with a score at or above `threshold` are flagged `match` by the
API; candidates between `cutoff` and `threshold` are returned unflagged and
are recorded here as `potential_match`. Either outcome needs a decision by
`staff_compliance`; `kyc.decide` refuses approval while the result is `match`.

Authentication: `Authorization: ApiKey <key>` (OpenSanctions API docs). The
same client works against a self-hosted *yente* instance (daily bulk data
under the commercial licence) by pointing `api_base` at it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from payintel.compliance import kyc
from payintel.core.clock import Clock
from payintel.core.errors import ConfigurationError, UpstreamError
from payintel.core.models.orgs import KycRecord, Organization
from payintel.core.settings import ComplianceSettings
from payintel.entitlements.model import Principal

SOURCE_PREFIX = "opensanctions"


class SanctionsUnavailable(UpstreamError):
    """The screening service answered with an error or could not be reached (→ 503)."""


@dataclass(frozen=True)
class Hit:
    query: str  # "org" | "beneficiary:<n>"
    query_name: str
    entity_id: str
    caption: str
    schema: str
    score: float
    match: bool
    datasets: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "query_name": self.query_name,
            "entity_id": self.entity_id,
            "caption": self.caption,
            "schema": self.schema,
            "score": self.score,
            "match": self.match,
            "datasets": self.datasets,
            "topics": self.topics,
        }


@dataclass(frozen=True)
class Screening:
    result: str  # clear | potential_match | match
    hits: list[Hit]
    checked_at: datetime
    source: str
    queries: int


def queries_for(org: Organization, rec: KycRecord | None) -> dict[str, dict[str, Any]]:
    """Query-by-example entities: the organisation and each beneficial owner."""
    props: dict[str, list[str]] = {"name": [org.legal_name]}
    if org.country:
        props["country"] = [org.country.lower()]
    if org.reg_number:
        props["registrationNumber"] = [org.reg_number]
    out: dict[str, dict[str, Any]] = {"org": {"schema": "Company", "properties": props}}
    for i, b in enumerate(rec.beneficiaries if rec else []):
        name = str(b.get("name", "")).strip()
        if not name:
            continue
        p: dict[str, list[str]] = {"name": [name]}
        if b.get("country"):
            p["nationality"] = [str(b["country"]).lower()]
        if b.get("birth_date"):
            p["birthDate"] = [str(b["birth_date"])]
        out[f"beneficiary:{i}"] = {"schema": "Person", "properties": p}
    return out


class OpenSanctionsClient:
    def __init__(
        self,
        api_key: str,
        *,
        settings: ComplianceSettings,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ConfigurationError(
                "OpenSanctions API key is not configured (PAYINTEL_SECRETS__OPENSANCTIONS_API_KEY)"
            )
        self._s = settings
        self._client = httpx.Client(
            base_url=settings.sanctions_api_base.rstrip("/"),
            headers={"Authorization": f"ApiKey {api_key}", "Accept": "application/json"},
            timeout=settings.sanctions_timeout_seconds,
            transport=transport,
        )

    @property
    def source(self) -> str:
        return f"{SOURCE_PREFIX}:{self._s.sanctions_dataset}"

    def match(self, queries: dict[str, dict[str, Any]], *, limit: int = 5) -> dict[str, Any]:
        params: dict[str, str | int | float] = {
            "algorithm": self._s.sanctions_algorithm,
            "threshold": self._s.sanctions_threshold,
            "cutoff": min(self._s.sanctions_cutoff, self._s.sanctions_threshold),
            "limit": limit,
        }
        try:
            r = self._client.post(
                f"/match/{self._s.sanctions_dataset}", params=params, json={"queries": queries}
            )
        except httpx.HTTPError as exc:
            raise SanctionsUnavailable(f"OpenSanctions unreachable: {exc}") from exc
        if r.status_code != 200:
            raise SanctionsUnavailable(
                f"OpenSanctions answered HTTP {r.status_code}", status=r.status_code
            )
        data: dict[str, Any] = r.json()
        if not isinstance(data.get("responses"), dict):
            raise SanctionsUnavailable("OpenSanctions answer has no responses")
        return data

    def close(self) -> None:
        self._client.close()


def hits_of(queries: dict[str, dict[str, Any]], data: dict[str, Any]) -> list[Hit]:
    hits: list[Hit] = []
    for key, q in queries.items():
        resp = data["responses"].get(key) or {}
        if int(resp.get("status", 200)) != 200:
            raise SanctionsUnavailable(f"query {key} failed with status {resp.get('status')}")
        names = q["properties"].get("name") or [""]
        for res in resp.get("results") or []:
            props = res.get("properties") or {}
            hits.append(
                Hit(
                    query=key,
                    query_name=str(names[0]),
                    entity_id=str(res.get("id", "")),
                    caption=str(res.get("caption", "")),
                    schema=str(res.get("schema", "")),
                    score=round(float(res.get("score", 0.0)), 4),
                    match=bool(res.get("match", False)),
                    datasets=[str(d) for d in res.get("datasets") or []],
                    topics=[str(t) for t in props.get("topics") or []],
                )
            )
    hits.sort(key=lambda h: (-h.score, h.query, h.entity_id))
    return hits


def classify(hits: list[Hit]) -> str:
    if any(h.match for h in hits):
        return "match"
    if hits:
        return "potential_match"
    return "clear"


def screen(
    session: Session,
    org: Organization,
    *,
    client: OpenSanctionsClient,
    principal: Principal,
    clock: Clock,
) -> tuple[KycRecord, Screening]:
    """Run the screening and record result, source, time and hits on the dossier."""
    rec = kyc.get_or_create(session, org)
    queries = queries_for(org, rec)
    data = client.match(queries)
    hits = hits_of(queries, data)
    now = clock.now()
    screening = Screening(classify(hits), hits, now, client.source, len(queries))
    kyc.record_sanctions(
        session,
        org,
        result=screening.result,
        source=screening.source,
        checked_at=now,
        principal=principal,
        clock=clock,
        details=[h.as_dict() for h in hits],
    )
    return rec, screening
