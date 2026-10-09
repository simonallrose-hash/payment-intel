"""Hosted checkout pages of providers (`reference/hosted_checkouts.yaml`, ADR-0032).

A walk never navigates outside the shop's eTLD+1 (ADR-0014 p. 9). When the
cart → checkout link, or the page the shop redirects to, is a documented
hosted checkout host of a provider, the walk stops with
`checkout/hosted_checkout_external` and names the provider instead of the
generic `checkout_not_found` / `navigation_error`. Only `verified` entries
match; `needs_verification` rows are kept for the record and the reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

VERIFIED = "verified"


@dataclass(frozen=True)
class HostedCheckout:
    host: str
    provider_id: str
    status: str
    source: str
    path_prefix: str = ""
    note: str = ""

    @property
    def verified(self) -> bool:
        return self.status == VERIFIED

    def matches(self, url: str) -> bool:
        """Exact host (no suffix match: `api.flutterwave.com` must not cover the whole
        domain) and, when set, the documented path prefix."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if host != self.host:
            return False
        return not self.path_prefix or (parts.path or "/").startswith(self.path_prefix)


@dataclass(frozen=True)
class HostedHit:
    """A hosted checkout seen during a walk: where, and whose."""

    host: str
    url: str
    provider_id: str
    via: str  # link | redirect


class HostedCheckouts:
    def __init__(self, entries: tuple[HostedCheckout, ...]) -> None:
        self.entries = entries
        self._verified = tuple(e for e in entries if e.verified)

    @classmethod
    def empty(cls) -> HostedCheckouts:
        return cls(())

    @classmethod
    def load(cls, path: Path | None = None) -> HostedCheckouts:
        # the loader imports this module; resolve its helpers at call time
        from payintel.core.reference_loader import REFERENCE_DIR, validate_schema

        path = path or REFERENCE_DIR / "hosted_checkouts.yaml"
        with path.open("r", encoding="utf-8") as fh:
            doc: dict[str, Any] = yaml.safe_load(fh)
        validate_schema(doc, "hosted_checkouts.schema.json", label=path.name)
        return cls(
            tuple(
                HostedCheckout(
                    host=str(h["host"]).lower(),
                    provider_id=str(h["provider_id"]),
                    status=str(h["status"]),
                    source=str(h["source"]),
                    path_prefix=str(h.get("path_prefix", "")),
                    note=str(h.get("note", "")),
                )
                for h in doc["hosts"]
            )
        )

    @property
    def verified(self) -> tuple[HostedCheckout, ...]:
        return self._verified

    def provider_ids(self) -> set[str]:
        return {e.provider_id for e in self.entries}

    def match(self, url: str) -> HostedCheckout | None:
        """The verified entry a URL lands on, or None (the longest path prefix wins)."""
        best: HostedCheckout | None = None
        for e in self._verified:
            if e.matches(url) and (best is None or len(e.path_prefix) > len(best.path_prefix)):
                best = e
        return best
