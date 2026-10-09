"""Stage-4 tests reuse the stage-3 application and world fixtures."""

from __future__ import annotations

from tests.stage3.conftest import (  # noqa: F401 - re-exported fixtures
    app,
    app_state,
    client,
    export_store,
    test_settings,
    world,
)
