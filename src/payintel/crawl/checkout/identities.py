"""Synthetic buyer identities (FR-CW-05, AS-21): `reference/synthetic_identities/<CC>.yaml`.

No real personal data: names are explicitly fictional, phone numbers come from
regulator-reserved fiction ranges where a country publishes one (GB, IE, US,
CA, AU) and otherwise from the company's own number (`identity.company_phone`),
addresses are fictional with a format-valid postcode for the country. The
e-mail is always on the company domain: `checkout-probe+<token>@<domain>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from payintel.core.reference_loader import REFERENCE_DIR, validate_schema

IDENTITIES_DIR = REFERENCE_DIR / "synthetic_identities"
DEFAULT_COUNTRY = "DE"
EMAIL_LOCAL_PART = "checkout-probe"


@dataclass(frozen=True)
class Identity:
    country: str
    locale: str
    first_name: str
    last_name: str
    email: str
    phone: str
    street: str
    house_number: str
    postcode: str
    city: str
    region: str
    region_code: str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    @property
    def street_line(self) -> str:
        return f"{self.street} {self.house_number}"

    @property
    def language(self) -> str:
        return self.locale.split("-")[0]

    @property
    def accept_language(self) -> str:
        return f"{self.locale},{self.language};q=0.8,en;q=0.5"

    def as_log(self) -> dict[str, str]:
        """What goes into the walk journal (FR-CW-05): the synthetic values themselves."""
        return {
            "country": self.country,
            "name": self.full_name,
            "email": self.email,
            "phone": self.phone,
            "address": f"{self.street_line}, {self.postcode} {self.city}",
        }


def probe_email(token: str, company_domain: str) -> str:
    safe = "".join(ch for ch in str(token) if ch.isalnum() or ch in "-_")[:64]
    return f"{EMAIL_LOCAL_PART}+{safe}@{company_domain}"


def _read(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        doc: dict[str, Any] = yaml.safe_load(fh)
    validate_schema(doc, "synthetic_identity.schema.json", label=path.name)
    if doc["country"] != path.stem:
        raise ValueError(f"{path.name}: country {doc['country']} does not match the file name")
    if doc["phone"]["kind"] == "fictional_range" and not doc["phone"].get("numbers"):
        raise ValueError(f"{path.name}: fictional_range phone without numbers")
    return doc


@lru_cache(maxsize=2)
def load_identity_sets(directory: Path = IDENTITIES_DIR) -> dict[str, dict[str, Any]]:
    return {p.stem: _read(p) for p in sorted(directory.glob("*.yaml"))}


class IdentityProvider:
    def __init__(
        self,
        *,
        company_domain: str,
        company_phone: str,
        directory: Path = IDENTITIES_DIR,
    ) -> None:
        self.company_domain = company_domain
        self.company_phone = company_phone
        self.sets = load_identity_sets(directory)
        if DEFAULT_COUNTRY not in self.sets:
            raise ValueError(f"synthetic_identities: {DEFAULT_COUNTRY}.yaml is required")

    def countries(self) -> set[str]:
        return set(self.sets)

    def resolve_country(self, country: str | None) -> str:
        cc = (country or "").upper()
        return cc if cc in self.sets else DEFAULT_COUNTRY

    def for_country(self, country: str | None, *, email_token: str, variant: int = 0) -> Identity:
        """Deterministic identity for a country; `variant` rotates names/addresses."""
        cc = self.resolve_country(country)
        doc = self.sets[cc]

        def pick(items: list[Any]) -> Any:
            return items[variant % len(items)]

        addr = pick(doc["addresses"])
        phone = (
            pick(doc["phone"]["numbers"])
            if doc["phone"]["kind"] == "fictional_range"
            else self.company_phone
        )
        return Identity(
            country=cc,
            locale=doc["locale"],
            first_name=pick(doc["first_names"]),
            last_name=pick(doc["last_names"]),
            email=probe_email(email_token, self.company_domain),
            phone=phone,
            street=addr["street"],
            house_number=str(addr["house_number"]),
            postcode=str(addr["postcode"]),
            city=addr["city"],
            region=str(addr.get("region", "")),
            region_code=str(addr.get("region_code", "")),
        )
