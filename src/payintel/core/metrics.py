"""Prometheus metrics (NFR-P-07: scan → event → alert latency; NFR-P-03..05: API latency).

The API serves them at `GET /metrics` behind `secrets.metrics_token`; workers
export them with `start_exporter(port)` (`payintel alerts dispatch
--metrics-port`). Metric objects live here, once per process; the modules
that observe them import this one.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)

SIX_HOURS = 6 * 3600

API_REQUEST_SECONDS = Histogram(
    "payintel_api_request_seconds",
    "Wall time of client API requests by route template (NFR-P-03/04/05).",
    ["route", "method", "status"],
    buckets=(0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0),
)

ALERT_LATENCY_SECONDS = Histogram(
    "payintel_alert_latency_seconds",
    "Scan finished → alert delivered, per channel (NFR-P-07: ≤ 6 h = 21600 s).",
    ["channel"],
    buckets=(30, 60, 300, 900, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400),
)

ALERT_BACKLOG_AGE_SECONDS = Gauge(
    "payintel_alert_backlog_age_seconds",
    "Age of the oldest change event with a pending delivery; 0 when nothing is pending.",
)

ALERT_DELIVERIES_TOTAL = Counter(
    "payintel_alert_deliveries_total",
    "Delivery attempts by channel and outcome (delivered / retry / failed).",
    ["channel", "outcome"],
)


def render(registry: CollectorRegistry = REGISTRY) -> tuple[bytes, str]:
    """Exposition body and its content type for the API endpoint."""
    return generate_latest(registry), CONTENT_TYPE_LATEST


def start_exporter(port: int, addr: str = "127.0.0.1") -> None:
    """Expose the process registry over HTTP (worker processes without the API)."""
    start_http_server(port, addr=addr)
