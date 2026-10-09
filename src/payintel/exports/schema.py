"""Field dictionary of every export type (FR-EX-01).

The same dictionary drives the Parquet schema, the CSV header and the
`*.schema.json` sidecar a client downloads next to the file. Profiles
`c1_basic` and `c1_full` differ in the column subset only; no column exists
for AS-23 data (FR-API-08).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from payintel.core.models.base import FieldProfile


@dataclass(frozen=True)
class Field:
    name: str
    type: str  # "string" | "int64" | "float64" | "bool" | "timestamp" | "date" | "list<string>"
    description: str
    full_only: bool = False

    def arrow(self) -> pa.DataType:
        return {
            "string": pa.string(),
            "int64": pa.int64(),
            "float64": pa.float64(),
            "bool": pa.bool_(),
            "timestamp": pa.timestamp("ms", tz="UTC"),
            "date": pa.date32(),
            "list<string>": pa.list_(pa.string()),
        }[self.type]


SNAPSHOT: tuple[Field, ...] = (
    Field("domain", "string", "Registrable domain (eTLD+1) of the store"),
    Field("as_of", "timestamp", "Time of the scan the row reflects (FR-API-10)"),
    Field("platform_id", "string", "E-commerce platform id from reference/platforms.yaml"),
    Field("platform_confidence", "string", "high | medium | low"),
    Field("country", "string", "ISO 3166-1 alpha-2 of the store (FR-DT-09)"),
    Field("country_confidence", "string", "high | medium | low"),
    Field("currency", "string", "Checkout currency, ISO 4217", full_only=True),
    Field("vertical_id", "string", "Vertical from reference/verticals.yaml", full_only=True),
    Field("checkout_status", "string", "Last checkout walk status (FR-CW-09)"),
    Field("coverage", "string", "homepage | product | cart | checkout | payment_step"),
    Field("acquirer_hidden", "bool", "Acquirer behind an orchestrator (FR-DT-06)", full_only=True),
    Field("provider_ids", "list<string>", "Current providers (ids)"),
    Field("providers", "list<string>", "`id:role:confidence` per provider"),
    Field(
        "providers_active_on_checkout",
        "list<string>",
        "Providers seen on the payment step",
        full_only=True,
    ),
    Field("method_ids", "list<string>", "Current payment methods (ids)"),
    Field("methods", "list<string>", "`id:type:confidence` per method"),
    Field(
        "checkout_psp_hosts", "list<string>", "Third-party PSP hosts on checkout", full_only=True
    ),
    Field("traffic_rank", "int64", "Tranco rank when known", full_only=True),
    Field("first_seen", "date", "Earliest first_seen among current providers"),
    Field("last_seen", "date", "Latest last_seen among current providers"),
    Field("last_checkout_scan_at", "timestamp", "Last successful checkout walk", full_only=True),
)

INCREMENT: tuple[Field, ...] = (
    Field("domain", "string", "Registrable domain"),
    Field(
        "event_type",
        "string",
        "provider_added | provider_removed | method_* | platform_changed | checkout_host_*",
    ),
    Field("entity", "string", "Provider / method / platform id the event is about"),
    Field("old_value", "string", "Previous value where applicable", full_only=True),
    Field("new_value", "string", "New value where applicable", full_only=True),
    Field("detected_at", "timestamp", "When the second confirming scan happened (FR-HI-04)"),
)

AGGREGATES: tuple[Field, ...] = (
    Field("country", "string", "ISO country or empty for all"),
    Field("platform_id", "string", "Platform or empty for all"),
    Field("provider_id", "string", "Provider id; `other` merges cells under the minimum size"),
    Field("stores", "int64", "Unique stores with the provider"),
    Field("share", "float64", "stores / stores in the (country, platform) cell"),
    Field("ci_low", "float64", "Wilson 95 % lower bound"),
    Field("ci_high", "float64", "Wilson 95 % upper bound"),
)

BY_TYPE: dict[str, tuple[Field, ...]] = {
    "full_snapshot": SNAPSHOT,
    "increment": INCREMENT,
    "market_aggregates": AGGREGATES,
}


def fields_for(export_type: str, profile: FieldProfile) -> tuple[Field, ...]:
    fields = BY_TYPE[export_type]
    if profile == FieldProfile.C1_FULL:
        return fields
    return tuple(f for f in fields if not f.full_only)


def arrow_schema(fields: tuple[Field, ...], metadata: dict[str, str]) -> pa.Schema:
    return pa.schema(
        [pa.field(f.name, f.arrow(), nullable=True) for f in fields], metadata=metadata
    )


def dictionary(fields: tuple[Field, ...], metadata: dict[str, str]) -> dict[str, Any]:
    """Content of the `.schema.json` sidecar (FR-EX-01)."""
    return {
        "export": metadata,
        "fields": [{"name": f.name, "type": f.type, "description": f.description} for f in fields],
    }
