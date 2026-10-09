"""Adapter registry (FR-CW-03): platform id → hints; `heuristic` for everything else.

Ten platforms carry adapters built from their open storefront sources (ADR-0014, ADR-0029,
ADR-0030); every other platform id in `reference/platforms.yaml` walks with the heuristic.
"""

from __future__ import annotations

from payintel.crawl.checkout.adapters.base import HEURISTIC, AdapterHints, merged
from payintel.crawl.checkout.adapters.bigcommerce import BIGCOMMERCE
from payintel.crawl.checkout.adapters.magento1 import MAGENTO1
from payintel.crawl.checkout.adapters.magento2 import MAGENTO2
from payintel.crawl.checkout.adapters.opencart import OPENCART
from payintel.crawl.checkout.adapters.oxid import OXID
from payintel.crawl.checkout.adapters.prestashop import PRESTASHOP
from payintel.crawl.checkout.adapters.shopify import SHOPIFY
from payintel.crawl.checkout.adapters.shopware5 import SHOPWARE5
from payintel.crawl.checkout.adapters.shopware6 import SHOPWARE6
from payintel.crawl.checkout.adapters.woocommerce import WOOCOMMERCE

ADAPTERS: tuple[AdapterHints, ...] = (
    WOOCOMMERCE,
    MAGENTO2,
    SHOPWARE6,
    PRESTASHOP,
    SHOPIFY,
    OPENCART,
    OXID,
    SHOPWARE5,
    MAGENTO1,
    BIGCOMMERCE,
)


def adapter_for(platform_id: str | None) -> AdapterHints:
    """Merged hints for a platform (adapter selectors first, heuristic after)."""
    pid = (platform_id or "").lower()
    for a in ADAPTERS:
        if pid in a.platform_ids:
            return merged(a)
    return HEURISTIC


__all__ = ["ADAPTERS", "HEURISTIC", "AdapterHints", "adapter_for", "merged"]
