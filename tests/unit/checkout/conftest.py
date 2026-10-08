"""Browser fixtures for guardrail tests: a shared Chromium per session, a fresh context per test."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import Page

from payintel.crawl.checkout.browser import BrowserPool, ContextOptions, WalkContext
from payintel.crawl.checkout.dictionary import load_dictionary
from payintel.crawl.checkout.guardrails import ClickPolicy, GuardedPage
from payintel.crawl.checkout.identities import IdentityProvider
from payintel.crawl.checkout.payment_values import load_payment_values
from payintel.crawl.checkout.types import ActionJournal, WalkFlags

UA = "PayIntelBot/1.0 (+https://example.invalid/bot)"


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def browser_pool(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[BrowserPool]:
    pool = BrowserPool(
        load_dictionary(),
        user_agent=UA,
        headless=True,
        restart_every=1000,
        har_dir=Path(tmp_path_factory.mktemp("har")),
    )
    await pool.start()
    try:
        yield pool
    finally:
        await pool.close()


@dataclass
class PageServer:
    """Serves `pages[path]` for any host; records every request (method, host, path, body)."""

    port: int
    pages: dict[str, str] = field(default_factory=dict)
    requests: list[tuple[str, str, str, str]] = field(default_factory=list)

    def rewrite(self, url: str) -> str | None:
        from urllib.parse import urlsplit

        u = urlsplit(url)
        if u.scheme not in {"http", "https"} or u.hostname in {"127.0.0.1", "localhost"}:
            return None
        return f"http://127.0.0.1:{self.port}{u.path or '/'}" + (f"?{u.query}" if u.query else "")

    def url(self, path: str = "/") -> str:
        return f"http://shop.test{path}"


def _handler(server: PageServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return

        def _serve(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            host = self.headers.get("x-forwarded-host", self.headers.get("host", ""))
            server.requests.append((self.command, host, self.path, body))
            path = self.path.split("?", 1)[0]
            html = server.pages.get(path)
            if html is None:
                self.send_response(404)
                self.end_headers()
                return
            data = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = _serve
        do_POST = _serve

    return Handler


@pytest.fixture
def page_server() -> Iterator[PageServer]:
    server = PageServer(port=0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(server))
    server.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest_asyncio.fixture(loop_scope="session")
async def walk_context(
    browser_pool: BrowserPool, page_server: PageServer
) -> AsyncIterator[WalkContext]:
    wc = await browser_pool.new_walk_context(
        ContextOptions(allow_private=True, rewrite=page_server.rewrite)
    )
    try:
        yield wc
    finally:
        await wc.close()


def make_guarded(
    page: Page, *, flags: WalkFlags | None = None, journal: ActionJournal | None = None
) -> GuardedPage:
    d = load_dictionary()
    ident = IdentityProvider(company_domain="payintel.example", company_phone="+44 20 7946 0000")
    return GuardedPage(
        page=page,
        policy=ClickPolicy(d),
        dictionary=d,
        identity=ident.for_country("DE", email_token="run-1"),
        payment_values=load_payment_values(),
        flags=flags or WalkFlags(),
        journal=journal or ActionJournal(lambda: 0),
        url_check=lambda url: None,
        action_timeout_ms=3_000,
        max_clicks=20,
    )
