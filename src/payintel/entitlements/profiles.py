"""Field profiles → response schemas (FR-API-08, FR-KYC-05, AS-23).

The C1 schemas are separate Pydantic models with `extra="forbid"`; a field
that is not declared cannot be serialised, so AS-23 data (third-party
non-PSP hosts, plugin and platform versions, internal scores) is absent
rather than hidden. `c2_risk` has no schema in this version (LR-16).
"""

from __future__ import annotations

from payintel.api.schemas import c1_basic, c1_full
from payintel.core.errors import C2DisabledError
from payintel.core.models.base import FieldProfile

# Keys that must never appear in any C1 payload (AC-07). The test
# `tests/integration/test_api_c1_forbidden_keys.py` scans serialised responses
# and exports recursively for these.
FORBIDDEN_C1_KEYS: frozenset[str] = frozenset(
    {
        "third_party_hosts",
        "checkout_hosts",
        "plugins",
        "platform_version",
        "version",
        "tech",
        "js_assets",
        "confidence_score",
        "misses",
        "confirmations",
        "artifact_prefix",
        "evidence_key",
        "scan_run_id",
        "host_id",
        "rule_id",
        "stop_detail",
        "page_url",
        "hosting_country",
        "asn",
        "last_resolved_ips",
    }
)


def store_schema(profile: FieldProfile) -> type[c1_basic.StoreBasic] | type[c1_full.StoreFull]:
    if profile == FieldProfile.C1_BASIC:
        return c1_basic.StoreBasic
    if profile == FieldProfile.C1_FULL:
        return c1_full.StoreFull
    raise C2DisabledError("c2_risk responses are not available in this version")


def change_schema(
    profile: FieldProfile,
) -> type[c1_basic.ChangeBasic] | type[c1_full.ChangeFull]:
    if profile == FieldProfile.C1_BASIC:
        return c1_basic.ChangeBasic
    if profile == FieldProfile.C1_FULL:
        return c1_full.ChangeFull
    raise C2DisabledError("c2_risk responses are not available in this version")
