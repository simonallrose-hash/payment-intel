"""Source downloaders for Discovery (FR-DS-01): Tranco, Common Crawl host graph, CT logs.

`payintel discovery fetch --source … --out DIR` writes a file that
`payintel discovery import --source …` ingests, so lineage stays the
existing one (`domain_source`, `import_batch`). The CT client polls a log
directly over RFC 6962 (`get-sth` / `get-entries`) and keeps a cursor file
per log, so a daily cron resumes where it stopped; the output is the
certstream-shaped JSONL `sources/ct.py` already parses.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import re
import struct
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.x509.oid import ExtensionOID, NameOID

TRANCO_URL = "https://tranco-list.eu/top-1m.csv.zip"
COMMONCRAWL_INDEX = "https://data.commoncrawl.org/projects/hyperlinkgraph/index.html"
CT_BATCH = 256
_X509_ENTRY = 0
_PRECERT_ENTRY = 1
_SLUG = re.compile(r"[^a-z0-9]+")


def make_client(*, timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True)


def fetch_tranco(client: httpx.Client, out_dir: Path, *, url: str = TRANCO_URL) -> Path:
    """Download today's Tranco top-1M zip and unpack the CSV into `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    r = client.get(url)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if not names:
            raise ValueError("tranco archive contains no CSV")
        data = zf.read(names[0])
    out = out_dir / f"tranco-{datetime.now(UTC):%Y-%m-%d}.csv"
    out.write_bytes(data)
    return out


def fetch_gzip(client: httpx.Client, url: str, out: Path) -> Path:
    """Stream a `.gz` file (Common Crawl `vertices.txt.gz`) and store it decompressed."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with client.stream("GET", url) as r:
        r.raise_for_status()
        with gzip.GzipFile(fileobj=_Stream(r)) as gz, out.open("wb") as fh:
            while chunk := gz.read(1 << 20):
                fh.write(chunk)
    return out


class _Stream(io.RawIOBase):
    """Adapts an httpx streaming response to the file API GzipFile expects."""

    def __init__(self, response: httpx.Response) -> None:
        self._it = response.iter_bytes()
        self._buf = b""

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray | memoryview) -> int:  # type: ignore[override]
        while not self._buf:
            try:
                self._buf = next(self._it)
            except StopIteration:
                return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


# --- Certificate Transparency (RFC 6962) ------------------------------------------------------


@dataclass(frozen=True)
class CtResult:
    log_url: str
    start: int
    next_index: int
    tree_size: int
    entries: int
    certificates: int
    precerts_skipped: int
    out: Path


def get_sth(client: httpx.Client, log_url: str) -> int:
    r = client.get(f"{log_url.rstrip('/')}/ct/v1/get-sth")
    r.raise_for_status()
    return int(r.json()["tree_size"])


def get_entries(client: httpx.Client, log_url: str, start: int, end: int) -> list[dict[str, str]]:
    r = client.get(f"{log_url.rstrip('/')}/ct/v1/get-entries", params={"start": start, "end": end})
    r.raise_for_status()
    entries: list[dict[str, str]] = r.json().get("entries", [])
    return entries


def domains_from_leaf(leaf_input_b64: str) -> list[str] | None:
    """DNS names of an `x509_entry` leaf; `None` for a precert (TBS without a signature,
    which `cryptography` cannot load: the final certificate follows in the same log)."""
    raw = base64.b64decode(leaf_input_b64)
    # MerkleTreeLeaf: version(1) leaf_type(1) timestamp(8) entry_type(2) ...
    if len(raw) < 12:
        return []
    (entry_type,) = struct.unpack(">H", raw[10:12])
    if entry_type == _PRECERT_ENTRY:
        return None
    if entry_type != _X509_ENTRY:
        return []
    length = int.from_bytes(raw[12:15], "big")
    der = raw[15 : 15 + length]
    try:
        cert = x509.load_der_x509_certificate(der)
    except ValueError:
        return []
    names: list[str] = []
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        names.extend(san.value.get_values_for_type(x509.DNSName))  # type: ignore[attr-defined]
    except x509.ExtensionNotFound:
        pass
    for attr in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME):
        value = attr.value if isinstance(attr.value, str) else attr.value.decode("utf-8", "ignore")
        if "." in value and value not in names:
            names.append(value)
    return names


def _cursor_path(out_dir: Path, log_url: str) -> Path:
    slug = _SLUG.sub("-", log_url.lower().replace("https://", "")).strip("-")
    return out_dir / f"ct-cursor-{slug}.txt"


def fetch_ct(
    client: httpx.Client,
    log_url: str,
    out_dir: Path,
    *,
    max_entries: int = 50_000,
    start: int | None = None,
    batch: int = CT_BATCH,
) -> CtResult:
    """Pull up to `max_entries` new leaves from one CT log into a certstream-shaped JSONL
    file; the cursor file remembers the next index for the following run."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cursor = _cursor_path(out_dir, log_url)
    tree_size = get_sth(client, log_url)
    if start is None:
        start = int(cursor.read_text().strip()) if cursor.exists() else max(0, tree_size - batch)
    index = start
    stop = min(tree_size, start + max_entries)
    out = out_dir / f"ct-{datetime.now(UTC):%Y-%m-%d}.jsonl"
    entries = certificates = precerts = 0
    with out.open("a", encoding="utf-8") as fh:
        while index < stop:
            end = min(index + batch, stop) - 1
            got = get_entries(client, log_url, index, end)
            if not got:
                break
            for entry in got:
                names = domains_from_leaf(entry.get("leaf_input", ""))
                entries += 1
                if names is None:
                    precerts += 1
                    continue
                if names:
                    certificates += 1
                    fh.write(json.dumps({"data": {"leaf_cert": {"all_domains": names}}}) + "\n")
            index += len(got)
    cursor.write_text(str(index), encoding="utf-8")
    return CtResult(log_url, start, index, tree_size, entries, certificates, precerts, out)
