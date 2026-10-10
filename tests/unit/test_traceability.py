"""`docs/traceability.md` stays true: every requirement row is present once, every row has a
known status and every cited file, line and function/test exists (debt sprint 3)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "traceability.md"
ROW = re.compile(r"^\| ((?:FR|NFR)-[A-Z0-9]+-\d+|(?:LR|AS|AC)-\d+) \| ")
REF = re.compile(r"`([^`]+)`")
# `path`, `path:12`, `path:12-20`, `path::name`, each optionally followed by free text
PATH_REF = re.compile(
    r"^(?P<path>(?:[\w.-]+/)*[\w.-]+\.(?:py|md|yaml|yml|toml|html|sql|txt|cfg|ini|json|sh|conf|dat|example|css|js)"
    r"|Makefile|Dockerfile[\w.-]*|(?:[\w.-]+/)+[\w.*-]*)"
    r"(?::(?P<line>\d+)(?:-\d+)?)?(?:::(?P<name>[A-Za-z_][A-Za-z0-9_]*))?(?:[ ,(].*)?$"
)
# continuation refs inside one cell: `:48 get_history` means line 48 of the previous path
CONT_REF = re.compile(
    r"^:(?P<line>\d+)(?:-\d+)?(?:\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*))?(?:[ (].*)?$"
)
EXPECTED_ROWS = 231
STATUSES = {"✅", "🟡", "🔴", "⛔", "🛠", "ℹ️", "⏳"}


def _rows() -> list[list[str]]:
    rows: list[list[str]] = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if ROW.match(line):
            cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
            rows.append(cells)
    return rows


def test_matrix_has_every_requirement_once() -> None:
    codes = [r[0] for r in _rows()]
    assert len(codes) == EXPECTED_ROWS, len(codes)
    assert len(set(codes)) == len(codes)
    assert "НЕ ПРОВЕРЕНО" not in DOC.read_text(encoding="utf-8")


def test_every_row_has_a_known_status() -> None:
    bad = [r[0] for r in _rows() if not any(r[2].startswith(s) for s in STATUSES)]
    assert not bad, bad


def _check(
    code: str, target: Path, line: str | None, name: str | None, problems: list[str]
) -> None:
    if not target.exists():
        problems.append(f"{code}: missing {target.relative_to(ROOT)}")
        return
    if target.is_file() and (line or name):
        text = target.read_text(encoding="utf-8", errors="ignore")
        if line and int(line) > text.count("\n") + 1:
            problems.append(f"{code}: {target.relative_to(ROOT)} has no line {line}")
        if name and not re.search(
            rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b", text, re.M
        ):
            problems.append(f"{code}: {target.relative_to(ROOT)} has no `{name}`")


def test_cited_files_lines_and_tests_exist() -> None:
    problems: list[str] = []
    for row in _rows():
        code = row[0]
        for cell in (row[3], row[4]):
            last: Path | None = None
            for ref in REF.findall(cell):
                ref = ref.replace("\\|", "|")
                cont = CONT_REF.match(ref)
                if cont and last is not None:
                    _check(code, last, cont.group("line"), cont.group("name"), problems)
                    continue
                m = PATH_REF.match(ref)
                if not m:
                    continue  # prose inside backticks (an endpoint, a threshold), not a file
                path = m.group("path")
                if ".." in path:
                    path = path.split("..")[0]  # `a.sql..b.sql` range: the first file
                if "*" in path:
                    path = path.split("*")[0]  # glob: its directory must exist
                    path = path if path.endswith("/") else path.rsplit("/", 1)[0]
                target = ROOT / path
                last = target if target.is_file() else None
                _check(code, target, m.group("line"), m.group("name"), problems)
    assert not problems, "\n".join(problems)
