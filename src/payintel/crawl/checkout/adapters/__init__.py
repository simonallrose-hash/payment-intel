"""Adapter registry (FR-CW-03): platform id → hints; `heuristic` for everything else."""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import HEURISTIC, AdapterHints, merged
from payintel.crawl.checkout.adapters.magento2 import MAGENTO2
from payintel.crawl.checkout.adapters.prestashop import PRESTASHOP
from payintel.crawl.checkout.adapters.shopware6 import SHOPWARE6
from payintel.crawl.checkout.adapters.woocommerce import WOOCOMMERCE

ADAPTERS: tuple[AdapterHints, ...] = (WOOCOMMERCE, MAGENTO2, SHOPWARE6, PRESTASHOP)


def adapter_for(platform_id: str | None) -> AdapterHints:
    """Merged hints for a platform (adapter selectors first, heuristic after)."""
    pid = (platform_id or "").lower()
    for a in ADAPTERS:
        if pid in a.platform_ids:
            return merged(a)
    return HEURISTIC


__all__ = ["ADAPTERS", "HEURISTIC", "AdapterHints", "adapter_for", "merged"]
