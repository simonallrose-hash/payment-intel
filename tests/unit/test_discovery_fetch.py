"""Source downloaders (FR-DS-01) against mocked HTTP: Tranco zip, CT log, gzip stream."""

from __future__ import annotations

import base64
import gzip
import io
import json
import struct
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from payintel.discovery import fetch
from payintel.discovery.sources.ct import parse_ct


def _cert_der(names: list[str]) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(7)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), False)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def _leaf(entry_type: int, payload: bytes) -> str:
    raw = b"\x00\x00" + struct.pack(">Q", 1700000000000) + struct.pack(">H", entry_type)
    if entry_type == 1:  # precert: issuer_key_hash + tbs
        raw += b"\x11" * 32
    raw += len(payload).to_bytes(3, "big") + payload
    return base64.b64encode(raw).decode()


def test_tranco_zip_is_unpacked_to_csv(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("top-1m.csv", "1,google.com\n2,shop.example\n")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(fetch.TRANCO_URL)
        return httpx.Response(200, content=buf.getvalue())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        out = fetch.fetch_tranco(client, tmp_path / "src")
    assert out.name.startswith("tranco-") and out.read_text().splitlines()[1] == "2,shop.example"


def test_gzip_stream_is_decompressed(tmp_path: Path) -> None:
    body = gzip.compress(b"1\tcom.example.shop\n2\tde.brand\n")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        out = fetch.fetch_gzip(
            client, "https://data.commoncrawl.org/x/vertices.txt.gz", tmp_path / "v.txt"
        )
    assert out.read_text().splitlines() == ["1\tcom.example.shop", "2\tde.brand"]


def test_ct_log_client_writes_certstream_lines_and_a_cursor(tmp_path: Path) -> None:
    der = _cert_der(["shop.example", "www.shop.example"])
    leaves = [_leaf(0, der), _leaf(1, b"\x30\x03\x02\x01\x01"), _leaf(0, b"garbage")]
    calls: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ct/v1/get-sth"):
            return httpx.Response(200, json={"tree_size": 1003})
        start, end = int(request.url.params["start"]), int(request.url.params["end"])
        calls.append((start, end))
        return httpx.Response(
            200,
            json={
                "entries": [
                    {"leaf_input": leaves[i - 1000], "extra_data": ""}
                    for i in range(start, min(end, 1002) + 1)
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r = fetch.fetch_ct(client, "https://ct.example/log", tmp_path, start=1000, batch=2)
        assert (r.start, r.next_index, r.tree_size) == (1000, 1003, 1003)
        assert (r.entries, r.certificates, r.precerts_skipped) == (3, 1, 1)
        assert calls == [(1000, 1001), (1002, 1002)]
        lines = r.out.read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["data"]["leaf_cert"]["all_domains"] == [
            "shop.example",
            "www.shop.example",
        ]
        assert {rec.hostname for rec in parse_ct(r.out)} == {"shop.example", "www.shop.example"}
        cursor = next(tmp_path.glob("ct-cursor-*.txt"))
        assert cursor.read_text() == "1003"
        # the next run resumes from the cursor and finds nothing new
        r2 = fetch.fetch_ct(client, "https://ct.example/log", tmp_path, batch=2)
        assert r2.start == 1003 and r2.entries == 0


def test_domains_from_leaf_handles_short_and_unknown_entries() -> None:
    assert fetch.domains_from_leaf(base64.b64encode(b"\x00" * 4).decode()) == []
    unknown = base64.b64encode(b"\x00\x00" + b"\x00" * 8 + struct.pack(">H", 9)).decode()
    assert fetch.domains_from_leaf(unknown) == []
