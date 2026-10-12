"""Raw-artefact retention sweep (LR-08, ADR-0036) against a fake S3 client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from typer.testing import CliRunner

from payintel.core.clock import FixedClock
from payintel.core.errors import StorageError
from payintel.core.retention import RAW_ARTIFACT_PREFIXES, sweep_raw_artifacts
from payintel.core.s3 import ObjectStore
from payintel.core.settings import Settings

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


class FakePaginator:
    def __init__(self, objects: dict[str, datetime], page_size: int) -> None:
        self._objects = objects
        self._page_size = page_size

    def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
        keys = sorted(k for k in self._objects if k.startswith(Prefix))
        pages = [keys[i : i + self._page_size] for i in range(0, len(keys), self._page_size)]
        return [
            {"Contents": [{"Key": k, "LastModified": self._objects[k]} for k in page]}
            for page in pages
        ] or [{}]


class FakeS3:
    """Just enough of the boto3 S3 client for `expire_prefix`."""

    def __init__(self, objects: dict[str, datetime], *, page_size: int = 500) -> None:
        self.objects = dict(objects)
        self.page_size = page_size
        self.delete_calls: list[list[str]] = []
        self.fail_keys: set[str] = set()

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_objects_v2"
        return FakePaginator(self.objects, self.page_size)

    def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:
        keys = [o["Key"] for o in Delete["Objects"]]
        assert Delete["Quiet"] is True
        self.delete_calls.append(keys)
        errors = [{"Key": k, "Code": "InternalError"} for k in keys if k in self.fail_keys]
        for k in keys:
            if k not in self.fail_keys:
                self.objects.pop(k, None)
        return {"Errors": errors} if errors else {}


def _objects(n_old: int, n_new: int, prefix: str = "light/") -> dict[str, datetime]:
    old = {f"{prefix}old/{i}": NOW - timedelta(days=91) for i in range(n_old)}
    new = {f"{prefix}new/{i}": NOW - timedelta(days=89) for i in range(n_new)}
    return {**old, **new}


def test_sweep_deletes_in_batches_of_1000_and_keeps_recent_objects() -> None:
    fake = FakeS3({**_objects(2300, 7), **_objects(3, 1, "checkout/"), "js/x": NOW})
    store = ObjectStore(fake)  # type: ignore[arg-type]
    counts = sweep_raw_artifacts(store, Settings(), clock=FixedClock(NOW))
    assert counts == {"light/": 2300, "checkout/": 3}
    assert [len(c) for c in fake.delete_calls] == [1000, 1000, 300, 3]
    assert set(fake.objects) == {f"light/new/{i}" for i in range(7)} | {"checkout/new/0", "js/x"}


def test_sweep_dry_run_and_custom_period() -> None:
    fake = FakeS3(_objects(5, 2))
    store = ObjectStore(fake)  # type: ignore[arg-type]
    counts = sweep_raw_artifacts(store, Settings(), clock=FixedClock(NOW), dry_run=True)
    assert counts == {"light/": 5, "checkout/": 0}
    assert fake.delete_calls == [] and len(fake.objects) == 7
    counts = sweep_raw_artifacts(store, Settings(), clock=FixedClock(NOW), days=30)
    assert counts == {"light/": 7, "checkout/": 0}
    assert fake.objects == {}


def test_sweep_raises_on_partial_delete_failure() -> None:
    fake = FakeS3(_objects(3, 0))
    fake.fail_keys = {"light/old/1"}
    store = ObjectStore(fake)  # type: ignore[arg-type]
    with pytest.raises(StorageError, match="InternalError"):
        sweep_raw_artifacts(store, Settings(), clock=FixedClock(NOW))


def test_prefixes_are_the_raw_artefact_roots() -> None:
    assert RAW_ARTIFACT_PREFIXES == ("light/", "checkout/")


def test_cli_artifacts_sweep_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from payintel import cli

    fake = FakeS3(_objects(4, 1))
    monkeypatch.setattr(cli, "get_settings", lambda: Settings())
    monkeypatch.setattr(cli, "make_object_store", lambda _s3: ObjectStore(fake))  # type: ignore[arg-type]
    monkeypatch.setattr(cli, "SYSTEM_CLOCK", FixedClock(NOW))
    result = CliRunner().invoke(cli.app, ["artifacts", "sweep", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "light/ would_delete=4" in result.output
    assert "checkout/ would_delete=0" in result.output
    assert len(fake.objects) == 5
    result = CliRunner().invoke(cli.app, ["artifacts", "sweep", "--days", "30"])
    assert result.exit_code == 0, result.output
    assert "light/ deleted=5" in result.output
    assert fake.objects == {}
