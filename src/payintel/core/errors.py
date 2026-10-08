"""Exception hierarchy. API layers map these to RFC 9457 problems by `code`."""

from __future__ import annotations


class PayIntelError(Exception):
    """Base class; `code` is the machine-readable error code."""

    code: str = "internal_error"

    def __init__(self, message: str = "", **context: object) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.context = context


class ConfigurationError(PayIntelError):
    code = "configuration_error"


class ValidationError(PayIntelError):
    code = "validation_error"


class ReferenceError_(ValidationError):
    """Invalid reference dictionary (FR-NR-*)."""

    code = "reference_invalid"


class RuleError(ValidationError):
    """Invalid detection rule (FR-DT-01, FR-NR-05)."""

    code = "rule_invalid"


class NotFoundError(PayIntelError):
    code = "not_found"


class ConflictError(PayIntelError):
    code = "conflict"


class ForbiddenError(PayIntelError):
    """Access outside entitlements / RBAC (FR-API-06, NFR-S-04)."""

    code = "forbidden"


class C2DisabledError(PayIntelError):
    """Phase-2 code reached while `feature_c2_enabled` is off (FR-KYC-07, LR-16)."""

    code = "c2_disabled"


class GuardrailViolation(PayIntelError):
    """A forbidden scanner action was attempted and blocked (FR-CW-04)."""

    code = "guardrail_violation"


class StorageError(PayIntelError):
    code = "storage_error"


class QualityGateError(PayIntelError):
    """Gold-set metrics below the release threshold (FR-QA-02)."""

    code = "quality_gate_failed"
