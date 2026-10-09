"""Load and validate reference dictionaries from `reference/*.yaml` (FR-NR-01..05).

Validation has two layers: JSON Schema (shape) and cross-checks (referential
integrity: parents and default providers exist, ids are unique, the 5.3
taxonomy is respected). Loading into the database is an upsert that bumps
`version` and writes an audit entry per changed row; rows are never deleted
(FR-NR-04).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.errors import ReferenceError_
from payintel.core.hosted_checkouts import HostedCheckouts
from payintel.core.models.base import PaymentMethodType, ProviderRole, ReferenceStatus
from payintel.core.models.reference import PaymentMethod, Platform, Provider, Vertical

REFERENCE_DIR = Path(__file__).resolve().parents[3] / "reference"
SCHEMA_DIR = REFERENCE_DIR / "schema"

MIN_PROVIDERS = 30  # FR-DT-03
MIN_PAYMENT_METHODS = 20  # FR-DT-03
VERTICALS_COUNT = 20  # FR-DT-10


@dataclass(frozen=True)
class StopReasonTaxonomy:
    """Parsed `stop_reasons.yaml` (FR-CW-13)."""

    steps: dict[str, tuple[str, ...]]
    any_step: tuple[str, ...]
    requires_detail: tuple[str, ...]

    def is_valid(self, step: str, reason: str, detail: str | None) -> bool:
        if reason in self.requires_detail and not (detail or "").strip():
            return False
        if reason in self.any_step:
            return step in self.steps
        return reason in self.steps.get(step, ())

    def all_codes(self) -> set[str]:
        codes = {r for reasons in self.steps.values() for r in reasons}
        return codes | set(self.any_step)


@dataclass
class ReferenceData:
    providers: list[dict[str, Any]]
    payment_methods: list[dict[str, Any]]
    platforms: list[dict[str, Any]]
    verticals: list[dict[str, Any]]
    stop_reasons: StopReasonTaxonomy
    problems: list[str] = field(default_factory=list)
    # ADR-0032: hosted checkout hosts of providers; `verified` rows drive the walker
    hosted_checkouts: HostedCheckouts = field(default_factory=HostedCheckouts.empty)


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ReferenceError_(f"{path.name}: top level must be a mapping")
    return data


def validate_schema(data: dict[str, Any], schema_name: str, *, label: str) -> None:
    schema = json.loads((SCHEMA_DIR / schema_name).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        messages = "; ".join(
            f"{'/'.join(str(p) for p in e.path)}: {e.message}" for e in errors[:10]
        )
        raise ReferenceError_(f"{label}: schema validation failed: {messages}")


def _unique_ids(rows: list[dict[str, Any]], label: str) -> set[str]:
    ids = [r["id"] for r in rows]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ReferenceError_(f"{label}: duplicate ids {sorted(dupes)}")
    return set(ids)


def load_reference(directory: Path = REFERENCE_DIR) -> ReferenceData:
    """Parse and validate all dictionaries; raises ReferenceError_ on any problem."""
    providers_doc = _read_yaml(directory / "providers.yaml")
    validate_schema(providers_doc, "providers.schema.json", label="providers.yaml")
    methods_doc = _read_yaml(directory / "payment_methods.yaml")
    validate_schema(methods_doc, "payment_methods.schema.json", label="payment_methods.yaml")
    platforms_doc = _read_yaml(directory / "platforms.yaml")
    validate_schema(platforms_doc, "platforms.schema.json", label="platforms.yaml")
    verticals_doc = _read_yaml(directory / "verticals.yaml")
    validate_schema(verticals_doc, "verticals.schema.json", label="verticals.yaml")
    stops_doc = _read_yaml(directory / "stop_reasons.yaml")
    validate_schema(stops_doc, "stop_reasons.schema.json", label="stop_reasons.yaml")

    providers = providers_doc["providers"]
    methods = methods_doc["payment_methods"]
    platforms = platforms_doc["platforms"]
    verticals = verticals_doc["verticals"]

    provider_ids = _unique_ids(providers, "providers.yaml")
    method_ids = _unique_ids(methods, "payment_methods.yaml")
    platform_ids = _unique_ids(platforms, "platforms.yaml")
    vertical_ids = _unique_ids(verticals, "verticals.yaml")
    del method_ids  # uniqueness is the only check needed for methods

    active_providers = [p for p in providers if p["status"] != ReferenceStatus.DEPRECATED.value]
    if len(active_providers) < MIN_PROVIDERS:
        raise ReferenceError_(
            f"providers.yaml: {len(active_providers)} active providers, "
            f"need >= {MIN_PROVIDERS} (FR-DT-03)"
        )
    if len(methods) < MIN_PAYMENT_METHODS:
        raise ReferenceError_(
            f"payment_methods.yaml: {len(methods)} methods, "
            f"need >= {MIN_PAYMENT_METHODS} (FR-DT-03)"
        )
    if len(verticals) != VERTICALS_COUNT:
        raise ReferenceError_(f"verticals.yaml: expected {VERTICALS_COUNT} verticals (FR-DT-10)")

    # Cross-checks (FR-NR-01, FR-NR-02, FR-NR-05)
    for p in providers:
        parent = p.get("parent_provider_id")
        if parent is not None and parent not in provider_ids:
            raise ReferenceError_(f"providers.yaml: {p['id']} parent {parent} does not exist")
        if parent == p["id"]:
            raise ReferenceError_(f"providers.yaml: {p['id']} is its own parent")
        ProviderRole(p["role"])
    for m in methods:
        dp = m.get("default_provider_id")
        if dp is not None and dp not in provider_ids:
            raise ReferenceError_(
                f"payment_methods.yaml: {m['id']} default provider {dp} does not exist"
            )
        PaymentMethodType(m["type"])
    for pl in platforms:
        parent = pl.get("parent_id")
        if parent is not None and parent not in platform_ids:
            raise ReferenceError_(f"platforms.yaml: {pl['id']} parent {parent} does not exist")
    for v in verticals:
        parent = v.get("parent_id")
        if parent is not None and parent not in vertical_ids:
            raise ReferenceError_(f"verticals.yaml: {v['id']} parent {parent} does not exist")

    stop_reasons = StopReasonTaxonomy(
        steps={k: tuple(v) for k, v in stops_doc["steps"].items()},
        any_step=tuple(stops_doc["any_step"]),
        requires_detail=tuple(stops_doc["requires_detail"]),
    )
    all_codes = stop_reasons.all_codes()
    for code in stop_reasons.requires_detail:
        if code not in all_codes:
            raise ReferenceError_(f"stop_reasons.yaml: requires_detail code {code} is unknown")
    if "other" not in stop_reasons.any_step or "other" not in stop_reasons.requires_detail:
        raise ReferenceError_("stop_reasons.yaml: `other` must be any-step and require detail")

    hosted = HostedCheckouts.load(directory / "hosted_checkouts.yaml")
    unknown = sorted(hosted.provider_ids() - provider_ids)
    if unknown:
        raise ReferenceError_(f"hosted_checkouts.yaml: unknown provider ids {unknown}")

    return ReferenceData(
        providers=providers,
        payment_methods=methods,
        platforms=platforms,
        verticals=verticals,
        stop_reasons=stop_reasons,
        hosted_checkouts=hosted,
    )


# --- database sync ----------------------------------------------------------


@dataclass
class SyncResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def _row_dict(obj: object, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: getattr(obj, k) for k in keys}


def _as_value(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def _normalise(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    values = {k: row.get(k) for k in keys}
    for list_key in ("aliases", "countries", "regions", "keywords"):
        if list_key in values and values[list_key] is None:
            values[list_key] = []
    if "status" in keys and values.get("status") is None:
        values["status"] = ReferenceStatus.ACTIVE.value
    return values


def _sync_rows(
    session: Session,
    model: type[Any],
    rows: list[dict[str, Any]],
    keys: tuple[str, ...],
    *,
    parent_key: str | None,
    actor: str,
    clock: Clock,
    result: SyncResult,
) -> None:
    """Two passes so self-referencing parents resolve in any file order.

    Pass 1 inserts missing rows with the parent cleared (one audit entry per
    insert); pass 2 sets parents on the rows just inserted (no version bump:
    it completes the insert) and updates existing rows whose content changed
    (version + 1, one audit entry with before/after).
    """
    object_type = model.__tablename__
    inserted_ids: set[str] = set()
    for row in rows:
        if session.get(model, row["id"]) is not None:
            continue
        values = _normalise(row, keys)
        if parent_key is not None:
            values[parent_key] = None
        session.add(model(**values))
        session.flush()
        audit.record(
            session,
            actor=actor,
            action="reference.insert",
            object_type=object_type,
            object_id=row["id"],
            after=_normalise(row, keys),
            clock=clock,
        )
        inserted_ids.add(row["id"])
        result.inserted += 1
    for row in rows:
        values = _normalise(row, keys)
        existing = session.get(model, row["id"])
        if existing is None:  # pragma: no cover - inserted above
            continue
        if row["id"] in inserted_ids:
            if parent_key is not None and values[parent_key] is not None:
                setattr(existing, parent_key, values[parent_key])
                session.flush()
            continue
        before = {k: _as_value(v) for k, v in _row_dict(existing, keys).items()}
        if before == values:
            result.unchanged += 1
            continue
        for k, v in values.items():
            setattr(existing, k, v)
        existing.version = existing.version + 1
        existing.updated_at = clock.now()
        session.flush()
        audit.record(
            session,
            actor=actor,
            action="reference.update",
            object_type=object_type,
            object_id=row["id"],
            before=before,
            after=values,
            clock=clock,
        )
        result.updated += 1


_PROVIDER_KEYS = (
    "id",
    "name",
    "aliases",
    "role",
    "owner_company",
    "parent_provider_id",
    "countries",
    "website",
    "status",
    "notes",
)
_METHOD_KEYS = ("id", "name", "type", "scheme_or_brand", "regions", "default_provider_id", "status")
_PLATFORM_KEYS = ("id", "name", "parent_id", "status")
_VERTICAL_KEYS = ("id", "name", "parent_id", "keywords", "status")


def sync_reference(
    session: Session, data: ReferenceData, *, actor: str = "seed", clock: Clock = SYSTEM_CLOCK
) -> SyncResult:
    """Upsert dictionaries into Postgres in dependency order, auditing every change."""
    result = SyncResult()
    _sync_rows(
        session,
        Provider,
        data.providers,
        _PROVIDER_KEYS,
        parent_key="parent_provider_id",
        actor=actor,
        clock=clock,
        result=result,
    )
    _sync_rows(
        session,
        Platform,
        data.platforms,
        _PLATFORM_KEYS,
        parent_key="parent_id",
        actor=actor,
        clock=clock,
        result=result,
    )
    _sync_rows(
        session,
        PaymentMethod,
        data.payment_methods,
        _METHOD_KEYS,
        parent_key=None,
        actor=actor,
        clock=clock,
        result=result,
    )
    _sync_rows(
        session,
        Vertical,
        data.verticals,
        _VERTICAL_KEYS,
        parent_key="parent_id",
        actor=actor,
        clock=clock,
        result=result,
    )
    return result
