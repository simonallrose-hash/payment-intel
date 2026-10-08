"""Hostname normalisation (FR-DS-04): lower-case, IDNA/punycode, no `www.`, no trailing dot."""

from __future__ import annotations

import re
from dataclasses import dataclass

from payintel.core.errors import ValidationError

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_HOSTNAME = 253
_IP_V4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


@dataclass(frozen=True)
class NormalizedHost:
    hostname: str  # ASCII (punycode), without `www.`
    labels: tuple[str, ...]

    @property
    def tld(self) -> str:
        return self.labels[-1]


def _to_ascii(label: str) -> str:
    try:
        return label.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationError(f"label {label!r} is not valid IDNA") from exc


def normalize_hostname(raw: str) -> NormalizedHost:
    """Return the canonical ASCII hostname or raise ValidationError.

    Accepts bare hostnames, `http(s)://host[:port]/path` and leading `*.` from
    certificate SANs. IP addresses, single-label names and names with invalid
    labels are rejected (they are never shops to scan).
    """
    value = raw.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if value.startswith("[") or value.count(":") > 1:
        raise ValidationError("IPv6 literals are not hostnames")
    value = value.split(":", 1)[0]
    value = value.removeprefix("*.").rstrip(".")
    if value.startswith("www."):
        value = value[4:]
    if not value:
        raise ValidationError("empty hostname")
    if _IP_V4_RE.match(value):
        raise ValidationError("IP addresses are not hostnames")
    labels = tuple(
        _to_ascii(label) if any(ord(c) > 127 for c in label) else label
        for label in value.split(".")
    )
    for label in labels:
        if not _LABEL_RE.match(label):
            raise ValidationError(f"invalid label {label!r} in {raw!r}")
    if len(labels) < 2:
        raise ValidationError(f"{raw!r} has no TLD")
    hostname = ".".join(labels)
    if len(hostname) > _MAX_HOSTNAME:
        raise ValidationError("hostname longer than 253 characters")
    if hostname.startswith("xn--") and labels[-1].startswith("xn--") and len(labels) == 2:
        pass  # IDN TLD with IDN SLD is fine
    return NormalizedHost(hostname=hostname, labels=labels)


def to_unicode(hostname: str) -> str:
    """Display form of a punycode hostname (never used as a key)."""
    try:
        return hostname.encode("ascii").decode("idna")
    except UnicodeError:
        return hostname
