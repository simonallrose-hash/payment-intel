"""Column helpers shared by the model modules."""

from __future__ import annotations

from sqlalchemy import Enum as SAEnum

from payintel.core.models.base import StrEnum


def enum_column(enum_type: type[StrEnum], *, name: str) -> SAEnum:
    """VARCHAR + CHECK constraint instead of a native PG enum.

    Keeps migrations reversible with plain DROP TABLE (NFR-M-04) and avoids
    ALTER TYPE when vocabularies grow.
    """
    return SAEnum(
        enum_type,
        name=name,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda e: [m.value for m in e],
        length=48,
        validate_strings=True,
    )
