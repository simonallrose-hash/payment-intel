"""Internal (staff) view of a store with every field (FR-ADM-04). Never served on /v1."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StoreInternal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_id: int
    domain: str
    hostname: str
    status: str
    optout: bool
    sources: list[str] = Field(default_factory=list)
    profile: dict[str, Any]
    providers: list[dict[str, Any]] = Field(default_factory=list)
    methods: list[dict[str, Any]] = Field(default_factory=list)
    third_party_hosts: list[dict[str, Any]] = Field(default_factory=list)
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    runs: list[dict[str, Any]] = Field(default_factory=list)
    plugins: list[str] = Field(default_factory=list)
    platform_version: str | None = None
    czds_only: bool = False
