#!/usr/bin/env python
"""NFR-P-06: time the pending export job(s) from request to file in S3 (AC-12).

    # request a full snapshot first (API or portal), then:
    uv run python scripts/bench_export.py --limit-seconds 900 --out bench-export.json

Runs one exporter pass (`payintel exports run --once --limit 1`) in a child process,
reports elapsed wall time, the child's peak RSS and the resulting export_job row,
and exits 1 when the job did not finish within `--limit-seconds` or failed.
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import select

from payintel.core.db import get_engine, session_scope
from payintel.core.models.base import ExportStatus
from payintel.core.models.exports import ExportJob


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit-seconds", type=float, default=900.0, help="NFR-P-06: 15 min")
    parser.add_argument("--out", type=Path, help="JSON report path")
    args = parser.parse_args()

    started = time.monotonic()
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "payintel.cli", "exports", "run", "--once", "--limit", "1"],
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.monotonic() - started
    peak_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024
    with session_scope(get_engine()) as session:
        job = session.execute(
            select(ExportJob).order_by(ExportJob.created_at.desc()).limit(1)
        ).scalar_one_or_none()
        report = {
            "requirement": "NFR-P-06",
            "elapsed_s": round(elapsed, 1),
            "limit_s": args.limit_seconds,
            "exporter_peak_rss_mb": round(peak_mb),
            "exporter_exit": proc.returncode,
            "job": None
            if job is None
            else {
                "id": str(job.id),
                "status": job.status.value,
                "rows": job.rows,
                "file_key": job.file_key,
            },
        }
    done = job is not None and job.status == ExportStatus.DONE
    report["passed"] = bool(done and proc.returncode == 0 and elapsed <= args.limit_seconds)
    mark = "PASS" if report["passed"] else "FAIL"
    job_info = report["job"] or {}
    print(
        f"{mark} NFR-P-06 export: elapsed={elapsed:.1f}s (limit {args.limit_seconds:.0f}s) "
        f"rows={job_info.get('rows')} status={job_info.get('status')} "
        f"peak_rss={peak_mb:.0f}MB exit={proc.returncode}"
    )
    if proc.returncode != 0:
        print(proc.stderr[-2000:], file=sys.stderr)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
