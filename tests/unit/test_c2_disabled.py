"""Phase-2 package cannot be imported while `feature_c2_enabled` is off (FR-KYC-07, LR-16)."""

from __future__ import annotations

import importlib
import sys

import pytest

from payintel.core.errors import C2DisabledError
from payintel.core.settings import get_settings


def _reimport() -> object:
    sys.modules.pop("payintel.c2", None)
    get_settings.cache_clear()
    return importlib.import_module("payintel.c2")


def test_c2_import_blocked_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAYINTEL_FLAGS__FEATURE_C2_ENABLED", raising=False)
    with pytest.raises(C2DisabledError):
        _reimport()
    assert "payintel.c2" not in sys.modules


def test_c2_import_allowed_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYINTEL_FLAGS__FEATURE_C2_ENABLED", "true")
    module = _reimport()
    assert module.__name__ == "payintel.c2"
    sys.modules.pop("payintel.c2", None)
