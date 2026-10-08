"""Feature flags with audit (FR-ADM-05).

Known flags and their defaults are declared in `FlagDefaults` (settings). The
`feature_flag` table stores overrides; every change is written to the audit log.
Flags that are not in `KNOWN_FLAGS` are rejected so that a typo cannot silently
enable or disable a safety switch.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from payintel.core import audit
from payintel.core.clock import SYSTEM_CLOCK, Clock
from payintel.core.errors import ValidationError
from payintel.core.models.audit import FeatureFlag
from payintel.core.settings import FlagDefaults

KNOWN_FLAGS: frozenset[str] = frozenset(FlagDefaults.model_fields.keys())

# Flags that act as emergency kill switches for scanner behaviour (AS-21, AS-25).
SAFETY_FLAGS: frozenset[str] = frozenset(
    {"allow_shipping_step_fill", "allow_account_registration", "allow_payment_field_fill"}
)


class FlagService:
    def __init__(
        self, session: Session, defaults: FlagDefaults, clock: Clock = SYSTEM_CLOCK
    ) -> None:
        self._session = session
        self._defaults = defaults
        self._clock = clock

    @staticmethod
    def _check_key(key: str) -> None:
        if key not in KNOWN_FLAGS:
            raise ValidationError(f"unknown feature flag: {key}", key=key)

    def is_enabled(self, key: str) -> bool:
        self._check_key(key)
        row = self._session.get(FeatureFlag, key)
        if row is None:
            return bool(getattr(self._defaults, key))
        return row.enabled

    def all(self) -> dict[str, bool]:
        return {key: self.is_enabled(key) for key in sorted(KNOWN_FLAGS)}

    def set(self, key: str, enabled: bool, *, actor: str, ip: str | None = None) -> FeatureFlag:
        """Set a flag and write an audit entry in the same transaction."""
        self._check_key(key)
        before = self.is_enabled(key)
        now: datetime = self._clock.now()
        row = self._session.get(FeatureFlag, key)
        if row is None:
            row = FeatureFlag(key=key, enabled=enabled, changed_by=actor, changed_at=now)
            self._session.add(row)
        else:
            row.enabled = enabled
            row.changed_by = actor
            row.changed_at = now
        self._session.flush()
        audit.record(
            self._session,
            actor=actor,
            action="feature_flag.set",
            object_type="feature_flag",
            object_id=key,
            before={"enabled": before},
            after={"enabled": enabled},
            ip=ip,
            clock=self._clock,
        )
        return row

    def history(self) -> list[FeatureFlag]:
        return list(self._session.execute(select(FeatureFlag).order_by(FeatureFlag.key)).scalars())
