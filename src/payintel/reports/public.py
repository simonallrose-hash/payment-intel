"""Public, free summary of a C Report (FR-RP-05): the same aggregates, no domains.

The summary keeps only what marketing publishes: per published cell the top
providers and payment methods with their Wilson intervals, the BNPL share
and the average number of providers per store. Rows merged into `other`
stay `other`; nothing below the cell floor (FR-RP-03, LR-19) is present
because the source `Report` already withheld it. The dict form is stored on
the report job and rendered by the public page and the public PDF.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from payintel.reports.aggregates import Report, ShareRow

TOP = 5


@dataclass(frozen=True)
class PublicRow:
    key: str
    share: float
    ci_low: float
    ci_high: float
    stores: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "share": round(self.share, 4),
            "ci_low": round(self.ci_low, 4),
            "ci_high": round(self.ci_high, 4),
            "stores": self.stores,
        }


@dataclass
class PublicCell:
    cell: str  # "DE|shopify"
    stores: int
    psp: list[PublicRow] = field(default_factory=list)
    methods: list[PublicRow] = field(default_factory=list)
    bnpl_share: float | None = None
    providers_per_store: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cell": self.cell,
            "stores": self.stores,
            "psp": [r.as_dict() for r in self.psp],
            "methods": [r.as_dict() for r in self.methods],
            "bnpl_share": None if self.bnpl_share is None else round(self.bnpl_share, 4),
            "providers_per_store": (
                None if self.providers_per_store is None else round(self.providers_per_store, 2)
            ),
        }


@dataclass
class PublicSummary:
    title: str
    countries: list[str]
    platforms: list[str]
    verticals: list[str]
    period: str
    generated_at: datetime
    stores: int
    min_cell: int
    cells_withheld: int
    cells: list[PublicCell] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "countries": self.countries,
            "platforms": self.platforms,
            "verticals": self.verticals,
            "period": self.period,
            "generated_at": self.generated_at.isoformat(),
            "stores": self.stores,
            "min_cell": self.min_cell,
            "cells_withheld": self.cells_withheld,
            "cells": [c.as_dict() for c in self.cells],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PublicSummary:
        cells = [
            PublicCell(
                cell=c["cell"],
                stores=int(c["stores"]),
                psp=[PublicRow(**r) for r in c.get("psp", [])],
                methods=[PublicRow(**r) for r in c.get("methods", [])],
                bnpl_share=c.get("bnpl_share"),
                providers_per_store=c.get("providers_per_store"),
            )
            for c in d.get("cells", [])
        ]
        return cls(
            title=str(d["title"]),
            countries=list(d.get("countries", [])),
            platforms=list(d.get("platforms", [])),
            verticals=list(d.get("verticals", [])),
            period=str(d.get("period", "")),
            generated_at=datetime.fromisoformat(d["generated_at"]),
            stores=int(d["stores"]),
            min_cell=int(d.get("min_cell", 30)),
            cells_withheld=int(d.get("cells_withheld", 0)),
            cells=cells,
        )


def _top(rows: list[ShareRow], group: str, *, top: int) -> list[PublicRow]:
    mine = [r for r in rows if r.group == group]
    named = sorted((r for r in mine if r.key != "other"), key=lambda r: (-r.stores, r.key))[:top]
    other = [r for r in mine if r.key == "other"]
    out = [PublicRow(r.key, r.share, r.ci[0], r.ci[1], r.stores) for r in named]
    out += [PublicRow("other", r.share, r.ci[0], r.ci[1], r.stores) for r in other]
    return out


def period_label(report: Report) -> str:
    s = report.spec
    start = s.period_start.isoformat() if s.period_start else "…"
    end = s.period_end.isoformat() if s.period_end else report.generated_at.date().isoformat()
    return f"{start} – {end}"


def from_report(report: Report, *, top: int = TOP) -> PublicSummary:
    s = report.spec
    published = sorted(c for c, n in report.cells.items() if n >= s.min_cell)
    bnpl = {r.group: r.share for r in report.bnpl_share if r.key == "bnpl"}
    cells = [
        PublicCell(
            cell=cell,
            stores=report.cells[cell],
            psp=_top(report.psp_share, cell, top=top),
            methods=_top(report.method_share, cell, top=top),
            bnpl_share=bnpl.get(cell),
            providers_per_store=report.providers_per_store.get(cell),
        )
        for cell in published
    ]
    title = f"Payment infrastructure of online stores: {', '.join(s.countries) or 'all countries'}"
    return PublicSummary(
        title=title,
        countries=list(s.countries),
        platforms=list(s.platforms),
        verticals=list(s.verticals),
        period=period_label(report),
        generated_at=report.generated_at,
        stores=report.total_stores,
        min_cell=s.min_cell,
        cells_withheld=report.suppressed_cells,
        cells=cells,
    )
