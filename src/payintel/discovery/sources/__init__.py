"""Domain sources (FR-DS-01, FR-DS-02). Each parser yields `SourceRecord`s from local files."""

from payintel.discovery.sources.base import SourceRecord, parse_source

__all__ = ["SourceRecord", "parse_source"]
