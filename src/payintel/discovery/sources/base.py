"""Common record type and dispatcher for discovery sources.

Sources are files on disk (downloaded by the operator or a cron job with its
own credentials); parsers never open network connections, so they are fully
testable offline. Hostnames are returned as found; normalisation and PSL
splitting happen once in `discovery.ingest`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from payintel.core.errors import ValidationError
from payintel.core.models.base import DomainSourceKind


@dataclass(frozen=True)
class SourceRecord:
    hostname: str
    rank: int | None = None


Parser = Callable[[Path], Iterator[SourceRecord]]
_PARSERS: dict[DomainSourceKind, Parser] = {}


def register(kind: DomainSourceKind) -> Callable[[Parser], Parser]:
    def deco(fn: Parser) -> Parser:
        _PARSERS[kind] = fn
        return fn

    return deco


def parse_source(kind: DomainSourceKind, path: Path) -> Iterator[SourceRecord]:
    # Import side effects register the parsers.
    from payintel.discovery.sources import commoncrawl, ct, czds, manual_csv, tranco  # noqa: F401

    try:
        parser = _PARSERS[kind]
    except KeyError as exc:
        raise ValidationError(f"no file parser for source {kind.value}") from exc
    if not path.is_file():
        raise ValidationError(f"{path} is not a file")
    return parser(path)
