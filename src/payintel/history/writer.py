"""Batched ClickHouse writer (6.5 decision 5): flush every 5 s or 10 000 rows.

Rows are immutable observations (FR-HI-01). The buffer is synchronous and
safe to call from the worker's event loop; `flush()` is also called at the
end of every worker batch and on shutdown, so nothing is lost on a clean exit.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from clickhouse_connect.driver.client import Client

OBS_COLUMNS: dict[str, list[str]] = {
    "obs_scan": [
        "scan_date",
        "scan_ts",
        "scan_run_id",
        "host_id",
        "etld1",
        "scan_type",
        "status",
        "coverage",
        "duration_ms",
        "blocked_by",
        "platform_id",
        "adapter",
        "scanner_version",
        "ruleset_version",
    ],
    "obs_tech": [
        "scan_date",
        "scan_ts",
        "host_id",
        "tech_id",
        "version",
        "confidence",
        "confidence_score",
        "rule_id",
        "rule_version",
        "page_type",
        "scan_run_id",
    ],
    "obs_provider": [
        "scan_date",
        "scan_ts",
        "host_id",
        "etld1",
        "provider_id",
        "role",
        "signal_type",
        "signal_value",
        "page_type",
        "page_url",
        "rule_id",
        "rule_version",
        "confidence",
        "confidence_score",
        "active_on_checkout",
        "evidence_key",
        "scan_run_id",
        "country",
        "platform_id",
        "vertical_id",
    ],
    "obs_payment_method": [
        "scan_date",
        "scan_ts",
        "host_id",
        "etld1",
        "method_id",
        "provider_id",
        "signal_type",
        "signal_value",
        "page_type",
        "page_url",
        "rule_id",
        "rule_version",
        "confidence",
        "confidence_score",
        "evidence_key",
        "scan_run_id",
        "country",
        "platform_id",
        "vertical_id",
    ],
    "obs_checkout_host": [
        "scan_date",
        "scan_ts",
        "host_id",
        "third_party_host",
        "third_party_etld1",
        "category",
        "resource_type",
        "initiator",
        "request_count",
        "first_seen",
        "scan_run_id",
    ],
    "obs_scan_stop": [
        "scan_date",
        "scan_ts",
        "scan_run_id",
        "host_id",
        "etld1",
        "platform_id",
        "adapter",
        "stop_step",
        "stop_reason",
        "stop_detail",
        "page_url",
        "element_selector",
        "element_text",
        "http_status",
        "steps.name",
        "steps.duration_ms",
        "scanner_version",
        "ruleset_version",
        "artifact_key",
    ],
}


@dataclass
class ObservationBuffer:
    client: Client | None
    batch_rows: int = 10_000
    batch_seconds: float = 5.0
    monotonic: Callable[[], float] = time.monotonic
    _rows: dict[str, list[list[Any]]] = field(default_factory=dict)
    _since: float | None = None
    flushed_rows: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, table: str, row: dict[str, Any]) -> None:
        """Thread-safe: persistence of concurrent scans runs in worker threads."""
        cols = OBS_COLUMNS[table]
        missing = [c for c in cols if c not in row]
        if missing:
            raise KeyError(f"{table}: missing columns {missing}")
        with self._lock:
            self._rows.setdefault(table, []).append([row[c] for c in cols])
            if self._since is None:
                self._since = self.monotonic()
            due = (
                self.pending >= self.batch_rows
                or self.monotonic() - self._since >= self.batch_seconds
            )
        if due:
            self.flush()

    @property
    def pending(self) -> int:
        return sum(len(v) for v in self._rows.values())

    def flush(self) -> int:
        with self._lock:
            rows_by_table = self._rows
            self._rows = {}
            self._since = None
        n = 0
        if self.client is not None:
            for table, rows in rows_by_table.items():
                if rows:
                    self.client.insert(table, rows, column_names=OBS_COLUMNS[table])
                    n += len(rows)
        else:
            n = sum(len(v) for v in rows_by_table.values())
        self.flushed_rows += n
        return n
