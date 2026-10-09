"""PDF output (FR-RP-04): charts and the methodology section, built with fpdf2.

The ТЗ names WeasyPrint + matplotlib; this build uses fpdf2 with bar charts
drawn from rectangles (ADR-0027): no system libraries (Pango/Cairo), no
HTML rendering surface, deterministic output in tests. Fonts are the vendored
DejaVu Sans (Bitstream Vera licence, `reports/fonts/LICENSE`) so that the
methodology text (Cyrillic references, typographic dashes) renders.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fpdf import FPDF

from payintel.reports import methodology
from payintel.reports.aggregates import Report, ShareRow
from payintel.reports.public import PublicCell, PublicRow, PublicSummary

FONTS = Path(__file__).parent / "fonts"
FONT = "DejaVu"
BAR_MAX_MM = 90.0
CHART_ROWS = 10


@dataclass(frozen=True)
class Bar:
    label: str
    value: float  # 0..1
    low: float
    high: float
    stores: int


class _Doc(FPDF):
    def __init__(self, *, title: str) -> None:
        super().__init__(orientation="P", unit="mm", format="A4")
        self.add_font(FONT, "", str(FONTS / "DejaVuSans.ttf"))
        self.add_font(FONT, "B", str(FONTS / "DejaVuSans-Bold.ttf"))
        self.set_title(title)
        self.set_author("PayIntel")
        self.set_creator("payintel.reports.pdf")
        self.set_auto_page_break(auto=True, margin=18)
        self.doc_title = title

    def header(self) -> None:
        self.set_font(FONT, "", 8)
        self.set_text_color(110, 110, 110)
        self.cell(0, 6, self.doc_title, align="L")
        self.ln(8)
        self.set_text_color(0, 0, 0)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font(FONT, "", 8)
        self.set_text_color(110, 110, 110)
        self.cell(0, 6, f"PayIntel · page {self.page_no()}", align="C")
        self.set_text_color(0, 0, 0)

    def h1(self, text: str) -> None:
        self.set_font(FONT, "B", 16)
        self.multi_cell(0, 9, text)
        self.ln(2)

    def h2(self, text: str) -> None:
        self.set_font(FONT, "B", 12)
        self.multi_cell(0, 7, text)
        self.ln(1)

    def para(self, text: str, *, size: float = 9.5) -> None:
        self.set_font(FONT, "", size)
        self.multi_cell(0, 5, text)
        self.ln(1)

    def kv(self, rows: Sequence[tuple[str, str]]) -> None:
        self.set_font(FONT, "", 9.5)
        for k, v in rows:
            self.cell(55, 5.5, k)
            self.multi_cell(0, 5.5, v, new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def grid(self, header: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
        widths = _widths(len(header))
        self.set_font(FONT, "B", 8.5)
        self.set_fill_color(235, 235, 235)
        for w, h in zip(widths, header, strict=True):
            self.cell(w, 6, str(h), border=1, fill=True)
        self.ln(6)
        self.set_font(FONT, "", 8.5)
        for row in rows:
            for w, v in zip(widths, row, strict=True):
                self.cell(w, 5.5, str(v), border=1)
            self.ln(5.5)
        self.ln(2)

    def bars(self, title: str, bars: Sequence[Bar]) -> None:
        """Horizontal bar chart: label, bar scaled to 100 %, Wilson interval as a thin line."""
        if not bars:
            return
        needed = 10 + 7 * len(bars)
        if self.get_y() + needed > self.h - 20:
            self.add_page()
        self.h2(title)
        self.set_font(FONT, "", 8.5)
        x0 = self.l_margin + 42
        for b in bars:
            y = self.get_y()
            self.cell(40, 6, b.label[:28])
            self.set_fill_color(70, 110, 170)
            self.rect(x0, y + 1, BAR_MAX_MM * max(0.0, min(1.0, b.value)), 4, style="F")
            self.set_draw_color(30, 30, 30)
            lo_x, hi_x = x0 + BAR_MAX_MM * b.low, x0 + BAR_MAX_MM * b.high
            self.line(lo_x, y + 3, hi_x, y + 3)
            self.line(lo_x, y + 1.5, lo_x, y + 4.5)
            self.line(hi_x, y + 1.5, hi_x, y + 4.5)
            self.set_x(x0 + BAR_MAX_MM + 3)
            self.cell(
                0, 6, f"{b.value * 100:.1f} % [{b.low * 100:.1f}–{b.high * 100:.1f}] n={b.stores}"
            )
            self.ln(7)
        self.ln(2)


def _widths(n: int) -> list[float]:
    total = 190.0
    first = 60.0 if n > 2 else total / n
    rest = (total - first) / max(1, n - 1)
    return [first] + [rest] * (n - 1)


def _bars_of(rows: list[ShareRow], group: str, *, top: int = CHART_ROWS) -> list[Bar]:
    mine = [r for r in rows if r.group == group]
    named = sorted((r for r in mine if r.key != "other"), key=lambda r: (-r.stores, r.key))[:top]
    other = [r for r in mine if r.key == "other"]
    return [Bar(r.key, r.share, r.ci[0], r.ci[1], r.stores) for r in [*named, *other]]


def _public_bars(rows: list[PublicRow]) -> list[Bar]:
    return [Bar(r.key, r.share, r.ci_low, r.ci_high, r.stores) for r in rows]


def write_pdf(report: Report, *, methodology_url: str) -> bytes:
    """Full report for analysts and clients: every published cell, flows, monthly, methodology."""
    s = report.spec
    doc = _Doc(title="PayIntel C Report")
    doc.add_page()
    doc.h1("PayIntel C Report")
    doc.kv(
        [
            ("Generated at", report.generated_at.isoformat()),
            ("Countries", ", ".join(s.countries) or "all"),
            ("Platforms", ", ".join(s.platforms) or "all"),
            ("Verticals", ", ".join(s.verticals) or "all"),
            ("Stores in scope", str(report.total_stores)),
            ("Cell floor", f"{s.min_cell} stores (FR-RP-03)"),
            ("Cells withheld", str(report.suppressed_cells)),
        ]
    )
    doc.h2("Cells")
    doc.grid(
        ["cell (country|platform)", "stores"],
        [(c, n if n >= s.min_cell else f"< {s.min_cell}") for c, n in sorted(report.cells.items())],
    )
    published = sorted(c for c, n in report.cells.items() if n >= s.min_cell)
    for cell in published:
        doc.add_page()
        doc.h1(f"Cell {cell} · {report.cells[cell]} stores")
        doc.bars("Share of stores by payment provider", _bars_of(report.psp_share, cell))
        doc.bars("Share of stores by payment method", _bars_of(report.method_share, cell))
        doc.bars("Stores offering BNPL", _bars_of(report.bnpl_share, cell))
        avg = report.providers_per_store.get(cell)
        if avg is not None:
            doc.para(f"Average number of providers per store: {avg:.2f}")
    if report.psp_flows:
        doc.add_page()
        doc.h2("PSP flows in the period (from → to)")
        doc.grid(["from", "to", "stores"], [(a, b, n) for a, b, n in report.psp_flows])
    if report.monthly:
        if not report.psp_flows:
            doc.add_page()
        doc.h2("Monthly dynamics (stores per provider)")
        doc.grid(["month", "provider", "stores"], [(m, p, n) for m, p, n in report.monthly])
    doc.add_page()
    lines = methodology.lines(report, methodology_url=methodology_url)
    doc.h1(lines[0])
    for line in lines[2:]:
        doc.para(line)
    return bytes(doc.output())


def write_public_pdf(summary: PublicSummary, *, methodology_url: str) -> bytes:
    """Marketing summary (FR-RP-05): top rows per published cell, no flows, no monthly rows."""
    doc = _Doc(title=summary.title)
    doc.add_page()
    doc.h1(summary.title)
    doc.para(
        f"Free summary of a PayIntel C Report · period {summary.period} · "
        f"generated {summary.generated_at.date().isoformat()}"
    )
    doc.kv(
        [
            ("Stores in scope", str(summary.stores)),
            ("Platforms", ", ".join(summary.platforms) or "all"),
            ("Verticals", ", ".join(summary.verticals) or "all"),
            ("Cell floor", f"{summary.min_cell} stores; {summary.cells_withheld} cells withheld"),
        ]
    )
    cell: PublicCell
    for cell in summary.cells:
        doc.h1(f"{cell.cell} · {cell.stores} stores")
        doc.bars("Top payment providers (share of stores)", _public_bars(cell.psp))
        doc.bars("Top payment methods (share of stores)", _public_bars(cell.methods))
        facts: list[str] = []
        if cell.bnpl_share is not None:
            facts.append(f"stores offering BNPL: {cell.bnpl_share * 100:.1f} %")
        if cell.providers_per_store is not None:
            facts.append(f"providers per store: {cell.providers_per_store:.2f}")
        if facts:
            doc.para("; ".join(facts).capitalize())
    doc.add_page()
    doc.h1("Methodology (short)")
    doc.para(
        "Data come from external observation of public store pages and a checkout walk that "
        "stops before any payment; no orders are placed. Only cells with at least "
        f"{summary.min_cell} stores are published; smaller rows are merged into 'other'. Shares "
        "carry 95 % Wilson score intervals. No store names or domains are part of this summary."
    )
    doc.para(f"Full methodology: {methodology_url}")
    return bytes(doc.output())
