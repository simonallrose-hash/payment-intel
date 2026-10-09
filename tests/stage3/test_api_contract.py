"""OpenAPI contract tests (FR-API-08, NFR-M-03): every documented /v1 operation
answers with a response that matches its schema, including 4xx problem
documents. Generation is derandomised, so the run is reproducible offline."""

from __future__ import annotations

from typing import Any

import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, Phase, settings
from schemathesis import Case

from tests.stage3.conftest import World, auth


@pytest.fixture
def contract_schema(app: FastAPI, world: World) -> Any:
    schema = schemathesis.openapi.from_asgi("/openapi.json", app)
    return schema.include(path_regex=r"^/v1/")


schema = schemathesis.pytest.from_fixture("contract_schema")


@schema.parametrize()
@settings(
    max_examples=4,
    deadline=None,
    derandomize=True,
    phases=(Phase.explicit, Phase.generate),
    suppress_health_check=list(HealthCheck),
)
def test_v1_operations_match_their_schema(case: Case[Any], world: World) -> None:
    response = case.call_and_validate(headers=auth(world.api_key))
    assert response.status_code < 500
    body = response.text
    for forbidden in ("third_party_hosts", "plugins", "platform_version", "internal_score"):
        assert forbidden not in body, f"{forbidden} leaked on {case.operation.label}"


def test_openapi_document_is_complete(app: FastAPI) -> None:
    doc = app.openapi()
    paths = {p for p in doc["paths"] if p.startswith("/v1/")}
    expected = {
        "/v1/stores",
        "/v1/stores/{domain}",
        "/v1/stores/{domain}/history",
        "/v1/changes",
        "/v1/stats/market-share",
        "/v1/watchlists",
        "/v1/watchlists/{watchlist_id}",
        "/v1/webhooks",
        "/v1/exports",
        "/v1/exports/{export_id}",
        "/v1/providers",
        "/v1/payment-methods",
    }
    missing = expected - paths
    assert not missing, missing
    for path, ops in doc["paths"].items():
        if not path.startswith("/v1/"):
            continue
        for method, op in ops.items():
            assert "403" in op["responses"], f"{method} {path} has no 403 documented"
            assert "429" in op["responses"], f"{method} {path} has no 429 documented"
