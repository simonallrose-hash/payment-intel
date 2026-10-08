"""Allowed values for payment fields (FR-CW-14, ADR-0003): `reference/test_payment_values.yaml`.

The file is the only source of card numbers. On load every `documented`
number must pass Luhn (they are scheme/PSP test cards) and every
`luhn_invalid` number must fail it; `preferred` must be a subset of both lists.
Nothing here generates numbers.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from payintel.core.reference_loader import REFERENCE_DIR, validate_schema


def luhn_valid(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass(frozen=True)
class TestPaymentValues:
    numbers: frozenset[str]  # every value the guardrail may let through
    documented: dict[str, str]  # number → brand
    luhn_invalid: tuple[str, ...]
    preferred: tuple[str, ...]
    expiry_month: str
    expiry_year_offset: int
    cvc_default: str
    cvc_amex: str
    holder_name: str
    client_side_tokenizers: frozenset[str]
    sources: dict[str, str]  # number → documentation URL

    def allowed(self, value: str) -> bool:
        """Byte-for-byte membership; spaces/dashes are not tolerated on purpose."""
        return value in self.numbers

    def number(self, *, brand: str | None = None) -> str:
        if brand:
            for n, b in self.documented.items():
                if b == brand:
                    return n
        return self.preferred[0]

    def cvc_for(self, number: str) -> str:
        return self.cvc_amex if self.documented.get(number) == "amex" else self.cvc_default

    def expiry(self, current_year: int) -> tuple[str, str, str]:
        """(MM, YY, YYYY) for a future year."""
        year = current_year + self.expiry_year_offset
        return self.expiry_month, f"{year % 100:02d}", str(year)


def _load(path: Path) -> TestPaymentValues:
    with path.open("r", encoding="utf-8") as fh:
        doc: dict[str, Any] = yaml.safe_load(fh)
    validate_schema(doc, "test_payment_values.schema.json", label=path.name)
    documented = {str(c["number"]): str(c["brand"]) for c in doc["cards"]["documented"]}
    sources = {str(c["number"]): str(c["source"]) for c in doc["cards"]["documented"]}
    invalid = tuple(str(n) for n in doc["cards"]["luhn_invalid"])
    for n in documented:
        if not luhn_valid(n):
            raise ValueError(f"{path.name}: documented test number {n} fails Luhn")
    for n in invalid:
        if luhn_valid(n):
            raise ValueError(f"{path.name}: {n} listed as luhn_invalid passes Luhn")
    numbers = frozenset(documented) | frozenset(invalid)
    preferred = tuple(str(n) for n in doc["preferred"])
    for n in preferred:
        if n not in numbers:
            raise ValueError(f"{path.name}: preferred number {n} is not in the lists")
    return TestPaymentValues(
        numbers=numbers,
        documented=documented,
        luhn_invalid=invalid,
        preferred=preferred,
        expiry_month=str(doc["expiry"]["month"]),
        expiry_year_offset=int(doc["expiry"]["year_offset_years"]),
        cvc_default=str(doc["cvc"]["default"]),
        cvc_amex=str(doc["cvc"]["amex"]),
        holder_name=str(doc["holder_name"]),
        client_side_tokenizers=frozenset(doc["client_side_tokenizers"]),
        sources=sources,
    )


@lru_cache(maxsize=2)
def load_payment_values(
    path: Path = REFERENCE_DIR / "test_payment_values.yaml",
) -> TestPaymentValues:
    return _load(path)
