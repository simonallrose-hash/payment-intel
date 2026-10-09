"""CSV output (FR-RP-04): a ZIP with one CSV per metric and methodology.txt."""

from __future__ import annotations

import csv
import io
import zipfile

from payintel.reports import methodology
from payintel.reports.aggregates import Report, ShareRow


def _share_csv(rows: list[ShareRow], key_header: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["cell", key_header, "stores", "cell_total", "share", "ci_low", "ci_high"])
    for r in rows:
        lo, hi = r.ci
        w.writerow([r.group, r.key, r.stores, r.total, f"{r.share:.4f}", f"{lo:.4f}", f"{hi:.4f}"])
    return buf.getvalue()


def write_csv_zip(report: Report, *, methodology_url: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if report.psp_share:
            z.writestr("psp_share.csv", _share_csv(report.psp_share, "provider_id"))
        if report.method_share:
            z.writestr("method_share.csv", _share_csv(report.method_share, "method_id"))
        if report.bnpl_share:
            z.writestr("bnpl_share.csv", _share_csv(report.bnpl_share, "bnpl"))
        if report.providers_per_store:
            s = io.StringIO()
            w = csv.writer(s, lineterminator="\n")
            w.writerow(["cell", "avg_providers"])
            for cell, v in report.providers_per_store.items():
                w.writerow([cell, v])
            z.writestr("providers_per_store.csv", s.getvalue())
        if report.psp_flows:
            s = io.StringIO()
            w = csv.writer(s, lineterminator="\n")
            w.writerow(["from_provider", "to_provider", "stores"])
            w.writerows(report.psp_flows)
            z.writestr("psp_flows.csv", s.getvalue())
        if report.monthly:
            s = io.StringIO()
            w = csv.writer(s, lineterminator="\n")
            w.writerow(["month", "provider_id", "stores"])
            w.writerows(report.monthly)
            z.writestr("monthly.csv", s.getvalue())
        z.writestr(
            "methodology.txt",
            "\n".join(methodology.lines(report, methodology_url=methodology_url)) + "\n",
        )
    return buf.getvalue()
