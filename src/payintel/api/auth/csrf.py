"""CSRF: per-session token, required in every state-changing portal form (NFR-S-05)."""

from __future__ import annotations

import hmac

from payintel.core.errors import ForbiddenError


class CsrfError(ForbiddenError):
    code = "csrf_failed"


def check(expected: str, submitted: str | None) -> None:
    if not submitted or not hmac.compare_digest(expected, submitted):
        raise CsrfError("CSRF token missing or invalid")
