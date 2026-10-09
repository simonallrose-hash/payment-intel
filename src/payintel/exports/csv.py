"""CSV (UTF-8) writer: one comment line with the export metadata, then the header.

List columns are joined with `|`. The leading `# key=value ...` line is the
file-level metadata CSV lacks natively; readers skip it with `comment="#"`.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any

from payintel.exports.schema import Field


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_csv(
    rows: list[dict[str, Any]], fields: tuple[Field, ...], metadata: dict[str, str]
) -> bytes:
    buf = io.StringIO()
    buf.write("# " + " ".join(f"{k}={v}" for k, v in metadata.items()) + "\n")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([f.name for f in fields])
    for r in rows:
        writer.writerow([_cell(r.get(f.name)) for f in fields])
    return buf.getvalue().encode("utf-8")


def read_csv(data: bytes) -> tuple[dict[str, str], list[dict[str, str]]]:
    text = data.decode("utf-8")
    first, _, rest = text.partition("\n")
    meta = dict(kv.split("=", 1) for kv in first.lstrip("# ").split(" ") if "=" in kv)
    reader = csv.DictReader(io.StringIO(rest))
    return meta, list(reader)
