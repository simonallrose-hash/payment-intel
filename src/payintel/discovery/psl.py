"""Public Suffix List with a repository snapshot (FR-DS-04).

The list is loaded from `reference/public_suffix_list.dat`, so eTLD+1 computation
never touches the network and is reproducible in tests. `payintel discovery
update-psl <file>` replaces the snapshot (monthly, per FR-DS-04); the loader
refuses a file that does not look like the PSL.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from publicsuffixlist import PublicSuffixList

from payintel.core.errors import ReferenceError_

PSL_PATH = Path(__file__).resolve().parents[3] / "reference" / "public_suffix_list.dat"
_MIN_RULES = 5_000


@dataclass(frozen=True)
class Split:
    hostname: str
    etld1: str
    public_suffix: str

    @property
    def is_subdomain(self) -> bool:
        return self.hostname != self.etld1

    @property
    def tld(self) -> str:
        return self.hostname.rsplit(".", 1)[-1]


class SuffixList:
    def __init__(self, path: Path = PSL_PATH) -> None:
        text = path.read_text(encoding="utf-8")
        rules = [ln for ln in text.splitlines() if ln and not ln.startswith("//")]
        if len(rules) < _MIN_RULES or "// ===BEGIN ICANN DOMAINS===" not in text:
            raise ReferenceError_(f"{path} does not look like the Public Suffix List")
        # Private-section rules (github.io, …) are kept: a shop on `brand.myshopify.com`
        # is a separate registrable name and must not collapse into `myshopify.com`.
        self._psl = PublicSuffixList(source=text.splitlines(keepends=True), accept_unknown=False)
        self.rule_count = len(rules)
        self.source_path = path

    def split(self, hostname: str) -> Split | None:
        """eTLD+1 for an already normalised hostname, or None when it is a bare suffix."""
        etld1 = self._psl.privatesuffix(hostname)
        if etld1 is None:
            return None
        suffix = self._psl.publicsuffix(hostname)
        return Split(hostname=hostname, etld1=etld1, public_suffix=suffix or "")


@lru_cache(maxsize=1)
def get_suffix_list() -> SuffixList:
    return SuffixList()
