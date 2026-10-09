"""Methodology section shipped with every report (FR-RP-04, LR-21, 2.5)."""

from __future__ import annotations

from payintel.reports.aggregates import Report


def lines(report: Report, *, methodology_url: str) -> list[str]:
    s = report.spec
    period = (
        f"{s.period_start.isoformat() if s.period_start else '…'} – "
        f"{s.period_end.isoformat() if s.period_end else report.generated_at.date().isoformat()}"
    )
    return [
        "Methodology",
        "",
        f"Generated at: {report.generated_at.isoformat()}",
        f"Scope: countries {', '.join(s.countries) or 'all'}; "
        f"platforms {', '.join(s.platforms) or 'all'}; "
        f"verticals {', '.join(s.verticals) or 'all'}; period {period}",
        f"Sample: {report.total_stores} e-commerce stores with a current profile in the scope; "
        f"{len(report.cells)} (country, platform) cells, {report.suppressed_cells} cells withheld.",
        "Data source: external observation only — homepage/product/cart pages and a checkout "
        "walk that stops at the payment step; no orders are placed and no payment is initiated.",
        "Coverage: a provider is counted when it is in the store's current state (two confirming "
        "scans for additions; removals after two consecutive misses). Acquirers behind an "
        "orchestrator or a hosted page are not visible (acquirer_hidden).",
        f"Cell suppression: only cells with at least {s.min_cell} stores are published; smaller "
        "provider/method rows are merged into 'other'; smaller cells are withheld entirely "
        "(LR-19).",
        "Confidence intervals: 95 % Wilson score intervals for shares (ci_low, ci_high).",
        "Known limitations (ТЗ 2.5): anti-bot protection blocks some checkouts; hosted checkouts "
        "outside the store's domain are not walked; platform detection relies on public "
        "signatures; detection rules carry a confidence level (high/medium/low) rather than a "
        "guarantee.",
        "Flows: a store counts in 'from → to' when provider A was removed and provider B added "
        "within the period.",
        "Monthly dynamics: unique stores per provider and month from the observation history "
        "(ClickHouse) or, when unavailable, cumulative first-seen months of current providers.",
        f"Full methodology: {methodology_url}",
        "Data are provided as observed and do not guarantee completeness (LR-21).",
    ]
