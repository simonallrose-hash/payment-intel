"""ClickHouse client and idempotent SQL migrations (AS-16, 5.2).

Migrations are numbered files in `clickhouse/migrations/NNNN_name.sql`. Each file
may contain several statements separated by `;` on its own line. Applied
versions are recorded in `schema_migrations`; every statement is written with
`IF NOT EXISTS` so re-applying is harmless even if the bookkeeping is lost.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from payintel.core.errors import ConfigurationError
from payintel.core.settings import ClickHouseSettings

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "clickhouse" / "migrations"
_FILE_RE = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")


def make_ch_client(settings: ClickHouseSettings, *, database: str | None = None) -> Client:
    """Connect over HTTP with a timeout; `database` overrides settings.database."""
    return clickhouse_connect.get_client(
        dsn=settings.url,
        username=settings.user,
        password=settings.password.get_secret_value(),
        database=database or settings.database,
        connect_timeout=10,
        send_receive_timeout=60,
    )


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    def statements(self) -> list[str]:
        text = self.path.read_text(encoding="utf-8")
        statements: list[str] = []
        for part in re.split(r";\s*\n", text):
            lines = [ln for ln in part.splitlines() if not ln.lstrip().startswith("--")]
            body = "\n".join(lines).strip()
            if body:
                statements.append(body)
        return statements


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_RE.match(path.name)
        if not match:
            raise ConfigurationError(f"bad ClickHouse migration file name: {path.name}")
        found.append(Migration(int(match["version"]), match["name"], path))
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise ConfigurationError("duplicate ClickHouse migration versions")
    return found


def _ensure_bookkeeping(client: Client) -> None:
    client.command(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version UInt32,
            name String,
            applied_at DateTime DEFAULT now()
        ) ENGINE = ReplacingMergeTree(applied_at) ORDER BY version
        """
    )


def applied_versions(client: Client) -> set[int]:
    _ensure_bookkeeping(client)
    rows: Sequence[Sequence[Any]] = client.query(
        "SELECT DISTINCT version FROM schema_migrations"
    ).result_rows
    return {int(r[0]) for r in rows}


def apply_migrations(client: Client, directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Apply pending migrations in order; returns the ones applied in this run."""
    done = applied_versions(client)
    applied: list[Migration] = []
    for migration in discover_migrations(directory):
        if migration.version in done:
            continue
        statements = migration.statements()
        if not statements:
            raise ConfigurationError(
                f"ClickHouse migration {migration.path.name} has no statements"
            )
        for statement in statements:
            client.command(statement)
        client.insert(
            "schema_migrations",
            [[migration.version, migration.name]],
            column_names=["version", "name"],
        )
        applied.append(migration)
    return applied


def drop_all(client: Client, directory: Path = MIGRATIONS_DIR) -> None:
    """Reverse of apply_migrations for tests and disposable environments.

    ClickHouse DDL has no transactional downgrade; the `DROP` list is derived
    from the `-- object: <name>` headers in each migration file (NFR-M-04:
    reversibility is explicit and tested).
    """
    for migration in reversed(discover_migrations(directory)):
        for obj in _declared_objects(migration):
            client.command(f"DROP TABLE IF EXISTS {obj}")
    client.command("DROP TABLE IF EXISTS schema_migrations")


def _declared_objects(migration: Migration) -> list[str]:
    objs: list[str] = []
    for line in migration.path.read_text(encoding="utf-8").splitlines():
        if line.startswith("-- object:"):
            objs.append(line.split(":", 1)[1].strip())
    return list(reversed(objs))
