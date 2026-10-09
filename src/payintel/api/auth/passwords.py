"""argon2id password hashing (NFR-S-02) and brute-force lockout (NFR-S-05)."""

from __future__ import annotations

from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from payintel.core.models.access import User

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError):
        return False


def is_locked(user: User, now: datetime) -> bool:
    return user.locked_until is not None and user.locked_until > now


def register_failure(user: User, *, now: datetime, max_attempts: int, window_minutes: int) -> None:
    """Count a failed login; the `max_attempts`-th failure locks for `window_minutes`."""
    user.failed_logins += 1
    if user.failed_logins >= max_attempts:
        user.locked_until = now + timedelta(minutes=window_minutes)
        user.failed_logins = 0


def register_success(user: User, *, now: datetime) -> None:
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
