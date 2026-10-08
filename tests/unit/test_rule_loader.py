"""Rule loader accepts shipped rules and rejects malformed ones (FR-DT-01/02, FR-NR-05)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from payintel.core.errors import RuleError
from payintel.core.models.base import SignalType
from payintel.core.reference_loader import load_reference
from payintel.detect.rules import RULES_DIR, load_rules


@pytest.fixture(scope="module")
def ruleset():  # type: ignore[no-untyped-def]
    return load_rules(reference=load_reference())


def test_shipped_rules_load(ruleset) -> None:  # type: ignore[no-untyped-def]
    assert len(ruleset.rules) >= 100
    providers_with_rules = {r.target_id for r in ruleset.rules if r.target_type.value == "provider"}
    assert len(providers_with_rules) >= 30  # FR-DT-03 starter set


def test_unverified_rules_are_disabled(ruleset) -> None:  # type: ignore[no-untyped-def]
    for r in ruleset.rules:
        if r.needs_verification:
            assert not r.enabled, r.rule_id  # brief rule 8


def test_all_signal_types_are_known(ruleset) -> None:  # type: ignore[no-untyped-def]
    assert {r.signal_type for r in ruleset.rules} <= set(SignalType)


def test_ruleset_version_is_content_hash(ruleset) -> None:  # type: ignore[no-untyped-def]
    again = load_rules(reference=load_reference())
    assert again.version == ruleset.version
    assert len(ruleset.version) == 16


# --- malformed rule files ----------------------------------------------------

BASE_RULE = {
    "id": "provider.stripe.network_host.99",
    "version": 1,
    "signal_type": "network_host",
    "pattern": "api.stripe.com",
    "weight": 0.5,
    "enabled": True,
}


def _write(tmp_path: Path, doc: dict, name: str = "stripe.yaml") -> Path:  # type: ignore[type-arg]
    d = tmp_path / "providers"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def _doc(**rule_overrides: object) -> dict:  # type: ignore[type-arg]
    return {
        "target_type": "provider",
        "target_id": "stripe",
        "rules": [{**BASE_RULE, **rule_overrides}],
    }


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"signal_type": "magic"}, "schema"),
        ({"weight": 1.5}, "schema"),
        ({"weight": -0.1}, "schema"),
        ({"pattern": ""}, "schema"),
        ({"enabled": True, "needs_verification": True}, "needs_verification"),
        ({"id": "stripe.network_host.1"}, "must start with"),
        ({"signal_type": "html_pattern", "pattern": "([unclosed"}, "invalid regex"),
        ({"pattern": "not a host!"}, "hostname"),
        ({"signal_type": "favicon", "pattern": "abc"}, "sha256"),
        ({"signal_type": "js_global", "pattern": "1bad-name"}, "dotted identifier"),
        ({"match": "fuzzy"}, "schema"),
        ({"page_scope": "footer"}, "schema"),
    ],
)
def test_invalid_rules_rejected(tmp_path: Path, override: dict, message: str) -> None:  # type: ignore[type-arg]
    path = _write(tmp_path, _doc(**override))
    with pytest.raises(RuleError, match=message):
        load_rules(tmp_path, reference=load_reference(), files=[path])


def test_unknown_target_rejected(tmp_path: Path) -> None:
    doc = {
        "target_type": "provider",
        "target_id": "unknown_psp",
        "rules": [{**BASE_RULE, "id": "provider.unknown_psp.network_host.1"}],
    }
    path = _write(tmp_path, doc, "unknown_psp.yaml")
    with pytest.raises(RuleError, match="FR-NR-05"):
        load_rules(tmp_path, reference=load_reference(), files=[path])


def test_duplicate_id_version_rejected(tmp_path: Path) -> None:
    doc = {"target_type": "provider", "target_id": "stripe", "rules": [BASE_RULE, dict(BASE_RULE)]}
    path = _write(tmp_path, doc)
    with pytest.raises(RuleError, match="duplicate"):
        load_rules(tmp_path, reference=load_reference(), files=[path])


def test_non_mapping_file_rejected(tmp_path: Path) -> None:
    d = tmp_path / "providers"
    d.mkdir()
    path = d / "bad.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(RuleError, match="mapping"):
        load_rules(tmp_path, reference=None, files=[path])


def test_every_shipped_file_has_a_schema_valid_source_or_note() -> None:
    """Provider rule files must cite the documentation they were written from (brief rule 8)."""
    for path in (RULES_DIR / "providers").glob("*.yaml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert doc.get("source"), f"{path.name} has no `source`"
