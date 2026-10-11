"""FR-LS-05: Wappalyzer-format loader (converter + CLI), without any vendor data."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from typer.testing import CliRunner

from payintel.cli import app
from payintel.core.reference_loader import load_reference
from payintel.detect import wappalyzer
from payintel.detect.engine import PageSignals, match_page
from payintel.detect.rules import load_rules

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "wappalyzer" / "technologies-sample.json"
)


def _convert() -> wappalyzer.ConvertResult:
    techs = wappalyzer.load_technologies([FIXTURE])
    return wappalyzer.convert(techs, reference=load_reference())


def test_names_map_to_reference_platforms_or_tech_slugs() -> None:
    result = _convert()
    by_name = {t.name: t for t in result.technologies}
    assert (by_name["Shopify"].target_type, by_name["Shopify"].target_id) == ("platform", "shopify")
    assert (
        by_name["Adobe Commerce"].target_id == "magento2"
    )  # alias of "Magento 2 / Adobe Commerce"
    chat = by_name["Example Chat Widget"]
    assert (chat.target_type, chat.target_id) == ("tech", "example_chat_widget")


def test_fields_become_signal_types_and_tags_are_stripped() -> None:
    result = _convert()
    chat = next(t for t in result.technologies if t.name == "Example Chat Widget")
    kinds = {(r.signal_type, r.pattern) for r in chat.rules}
    assert (
        "script_src",
        r"(?i)widget\.chat\.example/v(\d+(?:\.\d+)*)\.js",
    ) in kinds  # no \;version
    assert ("js_global", "ExampleChat") in kinds  # top-level global of ExampleChat.version
    assert any(s == "html_pattern" and re.escape("example-chat-key") in p for s, p in kinds)
    assert {s for s, _ in kinds} == {"script_src", "js_global", "html_pattern"}
    assert result.unsupported == {"dom": 1, "url": 1, "xhr": 1}  # counted, not converted
    shopify = next(t for t in result.technologies if t.name == "Shopify")
    header = next(r for r in shopify.rules if r.signal_type == "header" and "stage" in r.pattern)
    assert header.pattern == "x-shopify-stage: (?i)^(production|staging)$" and header.weight == 0.8
    assert {r.pattern for r in shopify.rules if r.signal_type == "cookie"} == {
        "_shopify_s",
        "cart_sig",
    }
    assert all(t.name != "Broken Regex Tech" for t in result.technologies)  # nothing usable
    assert result.invalid_patterns == 2


def test_written_files_load_with_the_regular_parser_and_stay_disabled(tmp_path: Path) -> None:
    reference = load_reference()
    result = _convert()
    files = wappalyzer.write_rule_files(result, tmp_path, source="fixture", reference=reference)
    assert {f.name for f in files} == {"shopify.yaml", "magento2.yaml", "example_chat_widget.yaml"}
    ruleset = load_rules(tmp_path, reference=reference, files=files)
    assert ruleset.rules and all(r.needs_verification and not r.enabled for r in ruleset.rules)
    assert ruleset.enabled() == []
    doc = yaml.safe_load((tmp_path / "shopify.yaml").read_text(encoding="utf-8"))
    assert doc["target_type"] == "platform" and "Shopify" in doc["source"]
    assert all(r["id"].startswith("platform.shopify.") for r in doc["rules"])
    # the converted meta/html patterns match the page they describe (once enabled by a person)
    sig = PageSignals(
        page_type="homepage",
        url="https://shop.example/",
        html='<html><head><meta name="generator" content="Shopify"><link rel="preconnect" '
        'href="https://cdn.shopify.com"></head></html>',
        script_srcs=["https://cdn.shopify.com/s/javascripts/app.js"],
        headers={"x-shopid": "123"},
        cookie_names={"_shopify_s"},
        js_globals={"Shopify"},
    )
    enabled = load_rules(tmp_path, reference=reference, files=files)
    forced = enabled.__class__(
        rules=tuple(
            r.__class__(**{**r.__dict__, "enabled": True, "needs_verification": False})
            for r in enabled.rules
        )
    )
    found = {
        f.rule.signal_type.value for f in match_page(forced, sig) if f.rule.target_id == "shopify"
    }
    assert found == {"script_src", "js_global", "header", "cookie", "html_pattern"}


def test_cli_imports_a_file_and_reports_counts(tmp_path: Path) -> None:
    out = tmp_path / "wappalyzer"
    r = CliRunner().invoke(
        app, ["rules-import-wappalyzer", str(FIXTURE), "--out", str(out), "--source", "fixture"]
    )
    assert r.exit_code == 0, r.output
    assert "4 technologies" in r.output or "3 technologies" in r.output
    assert "mapped to reference platforms" in r.output and "dom×1" in r.output
    assert sorted(p.name for p in out.glob("*.yaml")) == [
        "example_chat_widget.yaml",
        "magento2.yaml",
        "shopify.yaml",
    ]
    assert json.loads(FIXTURE.read_text())  # fixture stays valid JSON
