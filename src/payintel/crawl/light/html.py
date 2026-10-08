"""HTML feature extraction on the standard-library parser (no JS execution).

Produces the inputs for detection (FR-LS-05, FR-DT-02 signals available without
a browser), the e-commerce classifier (FR-DS-08) and country detection
(FR-DT-09). Deterministic and dependency-free.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

_WS = re.compile(r"\s+")
_JS_GLOBAL_RE = re.compile(
    r"(?:\bwindow\.|\bvar\s+|\blet\s+|\bconst\s+|^\s*)([A-Za-z_$][\w$]*)\s*(?:=|\()", re.M
)
_JS_DOTTED_RE = re.compile(r"\b([A-Z][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\s*\(")


@dataclass
class HtmlFeatures:
    base_url: str
    title: str = ""
    lang: str = ""
    hreflangs: list[str] = field(default_factory=list)
    scripts_src: list[str] = field(default_factory=list)
    inline_scripts: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    forms_action: list[str] = field(default_factory=list)
    iframes_src: list[str] = field(default_factory=list)
    favicon: str | None = None
    json_ld_types: list[str] = field(default_factory=list)
    json_ld_blocks: list[dict[str, Any]] = field(default_factory=list)
    button_texts: list[str] = field(default_factory=list)
    text: str = ""

    def js_globals(self) -> set[str]:
        """Identifiers assigned or called at top level in inline scripts (FR-LS-05)."""
        found: set[str] = set()
        for body in self.inline_scripts:
            found.update(m.group(1) for m in _JS_GLOBAL_RE.finditer(body))
            found.update(m.group(1) for m in _JS_DOTTED_RE.finditer(body))
        return found

    def external_hosts(self) -> set[str]:
        hosts: set[str] = set()
        own = urlsplit(self.base_url).hostname or ""
        for url in [*self.scripts_src, *self.iframes_src, *self.forms_action]:
            host = urlsplit(url).hostname
            if host and host != own:
                hosts.add(host.lower())
        return hosts

    def link_paths(self) -> set[str]:
        return {
            urlsplit(u).path.lower()
            + ("?" + urlsplit(u).query.lower() if urlsplit(u).query else "")
            for u in self.links
        }

    def external_link_hosts(self) -> set[str]:
        own = urlsplit(self.base_url).hostname or ""
        out: set[str] = set()
        for u in self.links:
            h = urlsplit(u).hostname
            if h and h.lower() != own.lower():
                out.add(h.lower())
        return out


class _Extractor(HTMLParser):
    _BUTTONISH = {"button", "a", "input", "span", "div"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.f = HtmlFeatures(base_url=base_url)
        self._stack: list[str] = []
        self._script_type = ""
        self._script_buf: list[str] = []
        self._capture_text: list[str] = []
        self._text_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        self._stack.append(tag)
        if tag == "html" and a.get("lang"):
            self.f.lang = a["lang"].strip().lower()
        elif tag == "title":
            self._in_title = True
        elif tag == "script":
            self._script_type = a.get("type", "").lower()
            src = a.get("src")
            if src:
                self.f.scripts_src.append(urljoin(self.f.base_url, src.strip()))
            self._script_buf = []
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            href = a.get("href", "").strip()
            if "alternate" in rel and a.get("hreflang"):
                self.f.hreflangs.append(a["hreflang"].lower())
            if href and ("icon" in rel) and self.f.favicon is None:
                self.f.favicon = urljoin(self.f.base_url, href)
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or a.get("http-equiv") or "").lower()
            if key and "content" in a:
                self.f.meta.setdefault(key, a["content"].strip())
        elif tag == "a" and a.get("href"):
            self.f.links.append(urljoin(self.f.base_url, a["href"].strip()))
        elif tag == "form" and a.get("action"):
            self.f.forms_action.append(urljoin(self.f.base_url, a["action"].strip()))
        elif tag == "iframe" and a.get("src"):
            self.f.iframes_src.append(urljoin(self.f.base_url, a["src"].strip()))
        if tag == "input" and a.get("type", "").lower() in {"submit", "button"} and a.get("value"):
            self.f.button_texts.append(_WS.sub(" ", a["value"]).strip().lower())
        if tag in self._BUTTONISH:
            self._capture_text.append("")
        # <input ...> and <img> are void elements; don't leave them on the stack
        if tag in {"input", "img", "br", "meta", "link", "hr"}:
            self._stack.pop()
            if tag == "input" and self._capture_text:
                self._capture_text.pop()

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            body = "".join(self._script_buf)
            if self._script_type in {"application/ld+json", "application/json+ld"}:
                self._add_json_ld(body)
            elif self._script_type in {"", "text/javascript", "application/javascript", "module"}:
                if body.strip():
                    self.f.inline_scripts.append(body)
            self._script_buf = []
            self._script_type = ""
        elif tag == "title":
            self._in_title = False
        if tag in self._BUTTONISH and self._capture_text:
            txt = _WS.sub(" ", self._capture_text.pop()).strip().lower()
            if 0 < len(txt) <= 60:
                self.f.button_texts.append(txt)
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1] == "script":
            self._script_buf.append(data)
            return
        if self._stack and self._stack[-1] == "style":
            return
        if self._in_title:
            self.f.title += data
        if self._capture_text:
            self._capture_text[-1] += data
        self._text_parts.append(data)

    def _add_json_ld(self, body: str) -> None:
        try:
            doc = json.loads(body)
        except json.JSONDecodeError:
            return
        items = doc if isinstance(doc, list) else [doc]
        for item in items:
            if isinstance(item, dict):
                self.f.json_ld_blocks.append(item)
                self._collect_types(item)

    def _collect_types(self, node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            t = node.get("@type")
            if isinstance(t, str):
                self.f.json_ld_types.append(t)
            elif isinstance(t, list):
                self.f.json_ld_types.extend(x for x in t if isinstance(x, str))
            for v in node.values():
                self._collect_types(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                self._collect_types(v, depth + 1)


def extract(html: str, base_url: str) -> HtmlFeatures:
    parser = _Extractor(base_url)
    parser.feed(html)
    parser.close()
    parser.f.title = _WS.sub(" ", parser.f.title).strip()
    parser.f.text = _WS.sub(" ", " ".join(parser._text_parts)).strip()
    return parser.f
