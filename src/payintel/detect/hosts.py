"""Third-party host categories on the checkout page (FR-DT-11).

`psp` is derived from the enabled provider rules (a host that matches any
`network_host` / `iframe_src` / `script_src` host pattern of a provider rule);
the other categories come from `reference/host_categories.yaml`; the rest is
`unknown`. Only `psp` hosts are visible in C1 (AS-23).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from payintel.core.models.base import RuleTargetType, SignalType
from payintel.core.reference_loader import REFERENCE_DIR, validate_schema
from payintel.detect.rules import RuleSet
from payintel.discovery.psl import get_suffix_list

CATEGORIES = ("psp", "analytics", "ads", "cdn", "tag_manager", "session_replay", "chat", "unknown")
_HOST_SIGNALS = {SignalType.NETWORK_HOST, SignalType.IFRAME_SRC, SignalType.SCRIPT_SRC}


@dataclass(frozen=True)
class HostCategory:
    host: str
    etld1: str
    category: str
    provider_id: str | None = None


def etld1_of(host: str) -> str:
    split = get_suffix_list().split(host)
    return split.etld1 if split is not None else host


def _suffix_match(host: str, pattern: str) -> bool:
    p = pattern[2:] if pattern.startswith("*.") else pattern
    return host == p or host.endswith("." + p)


@lru_cache(maxsize=2)
def _load_categories(path: Path) -> dict[str, tuple[str, ...]]:
    with path.open("r", encoding="utf-8") as fh:
        doc: dict[str, Any] = yaml.safe_load(fh)
    validate_schema(doc, "host_categories.schema.json", label=path.name)
    return {k: tuple(v) for k, v in doc["categories"].items()}


class HostCategorizer:
    def __init__(
        self, ruleset: RuleSet, path: Path = REFERENCE_DIR / "host_categories.yaml"
    ) -> None:
        self.categories = _load_categories(path)
        self.psp_hosts: list[tuple[str, str]] = []  # (host pattern, provider_id)
        for r in ruleset.enabled():
            if r.target_type != RuleTargetType.PROVIDER or r.signal_type not in _HOST_SIGNALS:
                continue
            if r.signal_type == SignalType.SCRIPT_SRC:
                h = urlsplit(r.pattern if "://" in r.pattern else "https://" + r.pattern).hostname
                if h and "." in h and r.pattern.split("/")[0] == h:
                    self.psp_hosts.append((h.lower(), r.target_id))
            elif r.match == "host_suffix":
                self.psp_hosts.append((r.pattern.lower(), r.target_id))

    def classify(self, host: str) -> HostCategory:
        h = host.lower().rstrip(".")
        for pattern, pid in self.psp_hosts:
            if _suffix_match(h, pattern):
                return HostCategory(h, etld1_of(h), "psp", pid)
        for cat, suffixes in self.categories.items():
            if any(_suffix_match(h, s) for s in suffixes):
                return HostCategory(h, etld1_of(h), cat)
        return HostCategory(h, etld1_of(h), "unknown")
