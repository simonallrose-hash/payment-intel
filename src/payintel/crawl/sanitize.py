"""PII minimisation before long-term storage (FR-LS-04, LR-08).

E-mail addresses and phone numbers are replaced in HTML, text and header
values before anything is written to S3 or the databases. The phone pattern is
deliberately strict (international prefix, parentheses or at least two
separated groups) so prices, SKUs, EANs and years survive; the tests pin that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"mailto:[^\"'\s>]+", re.I)
TEL_RE = re.compile(r"tel:[^\"'\s>]+", re.I)
# +49 30 1234567 | 0049 30 123 45 67 | +1 (415) 555-0132 | (030) 123 456 | 020 7946 0958
PHONE_RE = re.compile(
    r"(?<![\w.,])"
    r"(?:\+\d{1,3}[\s.-]?|00\d{1,3}[\s.-]?|\(\d{2,5}\)[\s.-]?|0\d{1,4}[\s.-])"
    r"(?:\(?\d{1,5}\)?[\s.-]?){2,6}\d"
    r"(?![\w])"
)
EMAIL_MASK = "[redacted-email]"
PHONE_MASK = "[redacted-phone]"


@dataclass(frozen=True)
class SanitizeStats:
    emails: int = 0
    phones: int = 0

    def __add__(self, other: SanitizeStats) -> SanitizeStats:
        return SanitizeStats(self.emails + other.emails, self.phones + other.phones)


def _digits(s: str) -> int:
    return sum(ch.isdigit() for ch in s)


def _phone_repl(m: re.Match[str]) -> str:
    value = m.group(0)
    n = _digits(value)
    if 7 <= n <= 15:
        return PHONE_MASK
    return value


def sanitize_text(text: str) -> tuple[str, SanitizeStats]:
    out, n_mail = MAILTO_RE.subn("mailto:" + EMAIL_MASK, text)
    out, n_mail2 = EMAIL_RE.subn(EMAIL_MASK, out)
    out, n_tel = TEL_RE.subn("tel:" + PHONE_MASK, out)
    before = out
    out = PHONE_RE.sub(_phone_repl, out)
    n_phone = before.count(PHONE_MASK) * 0 + out.count(PHONE_MASK) - n_tel
    return out, SanitizeStats(emails=n_mail + n_mail2, phones=n_tel + n_phone)


DROPPED_HEADERS = frozenset({"set-cookie", "cookie", "authorization", "proxy-authorization"})


def sanitize_headers(headers: dict[str, str]) -> tuple[dict[str, str], SanitizeStats]:
    """Mask PII in header values; credentials and cookies are never stored (LR-08)."""
    stats = SanitizeStats()
    clean: dict[str, str] = {}
    for k, v in headers.items():
        if k.lower() in DROPPED_HEADERS:
            continue
        value, s = sanitize_text(v)
        stats = stats + s
        clean[k] = value
    return clean, stats
