"""Phase 2 (C2 risk feed) — intentionally empty (FR-KYC-07, LR-16, FR-C2-*).

Importing this package raises `C2DisabledError` unless the global flag
`feature_c2_enabled` is on. Nothing from phase 2 is implemented in the first
version; see README.md in this directory for the extension points.
"""

from __future__ import annotations

from payintel.core.errors import C2DisabledError
from payintel.core.settings import get_settings

if not get_settings().flags.feature_c2_enabled:
    raise C2DisabledError("phase-2 package is disabled (feature_c2_enabled=false)")
