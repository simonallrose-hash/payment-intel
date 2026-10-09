"""XLSX output (FR-RP-04): one sheet per metric plus Methodology."""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.worksheet import Worksheet

from payintel.reports import methodology
from payintel.reports.aggregates import Report, ShareRow


def _share_sheet(sheet: Worksheet, rows: list[ShareRow], key_header: str) -> None:
    sheet.append(
        [
            "cell (country|platform)",
            key_header,
            "stores",
            "cell_total",
            "share",
            "ci_low",
            "ci_high",
        ]
    )
    for r in rows:
        lo, hi = r.ci
        sheet.append(
            [r.group, r.key, r.stores, r.total, round(r.share, 4), round(lo, 4), round(hi, 4)]
        )


def write_xlsx(report: Report, *, methodology_url: str) -> bytes:
    wb = Workbook()
    ws = wb.create_sheet("Summary", 0)
    wb.remove(wb.worksheets[1])
    ws.append(["PayIntel C Report"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws.append(["generated_at", report.generated_at.isoformat()])
    ws.append(["countries", ", ".join(report.spec.countries) or "all"])
    ws.append(["platforms", ", ".join(report.spec.platforms) or "all"])
    ws.append(["verticals", ", ".join(report.spec.verticals) or "all"])
    ws.append(["stores_in_scope", report.total_stores])
    ws.append(["min_cell_size", report.spec.min_cell])
    ws.append(["cells_withheld", report.suppressed_cells])
    ws.append([])
    ws.append(["cell (country|platform)", "stores"])
    for cell, n in sorted(report.cells.items()):
        ws.append([cell, n if n >= report.spec.min_cell else f"< {report.spec.min_cell}"])

    if report.psp_share:
        _share_sheet(wb.create_sheet("PSP share"), report.psp_share, "provider_id")
    if report.method_share:
        _share_sheet(wb.create_sheet("Payment methods"), report.method_share, "method_id")
    if report.bnpl_share:
        _share_sheet(wb.create_sheet("BNPL"), report.bnpl_share, "bnpl")
    if report.providers_per_store:
        s = wb.create_sheet("Providers per store")
        s.append(["cell (country|platform)", "avg_providers"])
        for cell, v in report.providers_per_store.items():
            s.append([cell, v])
    if report.psp_flows:
        s = wb.create_sheet("PSP flows")
        s.append(["from_provider", "to_provider", "stores"])
        for a, b, n in report.psp_flows:
            s.append([a, b, n])
    if report.monthly:
        s = wb.create_sheet("Monthly")
        s.append(["month", "provider_id", "stores"])
        for m, p, n in report.monthly:
            s.append([m, p, n])
    meth = wb.create_sheet("Methodology")
    for line in methodology.lines(report, methodology_url=methodology_url):
        meth.append([line])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
