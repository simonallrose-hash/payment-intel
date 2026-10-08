"""Repository hygiene: no secrets in code, `.env.example` has only empty values (NFR-S-06)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"sk_(live|test)_[0-9a-zA-Z]{20,}"),  # Stripe secret key
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),  # Slack
    re.compile(r"\b\d{9,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
]
SCAN_SUFFIXES = {".py", ".yaml", ".yml", ".toml", ".md", ".sql", ".json", ".ini", ".env", ".cfg"}


def test_env_example_has_no_values() -> None:
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert value == "", f"{key} must be empty in .env.example"


def test_no_secret_shaped_strings_in_tracked_files() -> None:
    offenders: list[str] = []
    for path in ROOT.rglob("*"):
        if any(
            part in {".venv", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
            for part in path.parts
        ):
            continue
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text) and path.name != "test_no_secrets.py":
                offenders.append(f"{path}: {pattern.pattern}")
    assert not offenders, offenders


def test_no_stub_markers_in_source() -> None:
    """Brief rule 7: no TODO / NotImplementedError / bare `pass` stubs in delivered code."""
    src = ROOT / "src" / "payintel"
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if "TODO" in stripped or "NotImplementedError" in stripped or stripped == "pass":
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {stripped}")
    assert not offenders, offenders
