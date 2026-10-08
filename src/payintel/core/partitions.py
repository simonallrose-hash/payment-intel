"""Monthly range partitions for `usage_log` (5.1).

The initial migration creates partitions for a fixed window plus a DEFAULT
partition; a scheduled job calls `ensure_usage_log_partitions` so that the next
months always exist before data arrives.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Connection, text

TABLE = "usage_log"


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def partition_name(year: int, month: int) -> str:
    return f"{TABLE}_y{year:04d}m{month:02d}"


def partition_ddl(year: int, month: int) -> str:
    start, end = month_bounds(year, month)
    return (
        f"CREATE TABLE IF NOT EXISTS {partition_name(year, month)} PARTITION OF {TABLE} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )


def months_from(start: date, count: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = start.year, start.month
    for _ in range(count):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def ensure_usage_log_partitions(
    conn: Connection, *, today: date, months_ahead: int = 3
) -> list[str]:
    created: list[str] = []
    for y, m in months_from(today.replace(day=1), months_ahead + 1):
        conn.execute(text(partition_ddl(y, m)))
        created.append(partition_name(y, m))
    return created
