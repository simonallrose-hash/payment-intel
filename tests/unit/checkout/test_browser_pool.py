"""BrowserPool: context isolation and restarts (FR-CW-01, FR-CW-10) and the remote
browser server mode (NFR-S-08)."""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from payintel.crawl.checkout.browser import BrowserPool, ContextOptions
from payintel.crawl.checkout.dictionary import load_dictionary
from tests.unit.checkout.conftest import UA, PageServer

PAGE = "<html><body><h1>shop</h1><script>document.cookie='sid=abc; path=/'</script></body></html>"


@pytest.mark.asyncio(loop_scope="session")
async def test_each_walk_gets_an_isolated_context(
    browser_pool: BrowserPool, page_server: PageServer
) -> None:
    """FR-CW-01: cookies and storage set during one walk are invisible to the next one."""
    page_server.pages["/"] = PAGE
    opts = ContextOptions(allow_private=True, rewrite=page_server.rewrite)
    first = await browser_pool.new_walk_context(opts)
    try:
        page = await first.new_page()
        await page.goto(page_server.url("/"))
        await page.evaluate("localStorage.setItem('k', 'v')")
        assert await first.cookie_names() == ["sid"]
        assert await page.evaluate("navigator.userAgent") == UA
    finally:
        await first.close()
    second = await browser_pool.new_walk_context(opts)
    try:
        page = await second.new_page()
        assert await second.cookie_names() == []
        await page.goto(page_server.url("/empty"))  # 404 page: nothing sets a cookie here
        assert await page.evaluate("localStorage.getItem('k')") is None
        assert await second.cookie_names() == []
    finally:
        await second.close()


@pytest.mark.asyncio(loop_scope="session")
async def test_browser_restarts_every_n_walks_and_is_headless(
    page_server: PageServer, tmp_path: Path
) -> None:
    """FR-CW-10 / FR-CW-01: a plain headless launch, restarted after `restart_every` walks."""
    pool = BrowserPool(load_dictionary(), user_agent=UA, headless=True, restart_every=2)
    await pool.start()
    try:
        assert pool.headless and pool.browser.is_connected()
        opts = ContextOptions(allow_private=True, rewrite=page_server.rewrite)
        for _ in range(2):
            wc = await pool.new_walk_context(opts)
            await wc.close()
        assert pool.restarts == 0 and pool.walks_since_restart == 2
        wc = await pool.new_walk_context(opts)
        assert pool.restarts == 1 and pool.walks_since_restart == 1
        page = await wc.new_page()
        page_server.pages["/"] = PAGE
        await page.goto(page_server.url("/"))
        assert await BrowserPool.js_heap_mb(page) > 0  # the FR-CW-10 memory guard reads this
        await wc.close()
    finally:
        await pool.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest_asyncio.fixture(loop_scope="session")
async def browser_server() -> AsyncIterator[str]:
    """`playwright run-server` as a separate process, the way the `browser` container runs it."""
    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "playwright",
            "run-server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    line = await asyncio.get_running_loop().run_in_executor(None, proc.stdout.readline)
    try:
        assert "Listening on" in line, line
        yield f"ws://127.0.0.1:{port}/"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.mark.asyncio(loop_scope="session")
async def test_pool_connects_to_a_remote_browser_server(
    browser_server: str, page_server: PageServer
) -> None:
    """NFR-S-08: no Chromium in the worker process; walks run on the browser server."""
    pool = BrowserPool(
        load_dictionary(), user_agent=UA, restart_every=1, ws_endpoint=browser_server
    )
    await pool.start()
    try:
        assert pool.ws_endpoint == browser_server and pool.browser.is_connected()
        page_server.pages["/"] = PAGE
        wc = await pool.new_walk_context(
            ContextOptions(allow_private=True, rewrite=page_server.rewrite)
        )
        page = await wc.new_page()
        await page.goto(page_server.url("/"))
        assert "shop" in await page.content() and await wc.cookie_names() == ["sid"]
        assert page_server.requests and page_server.requests[0][2] == "/"
        await wc.close()
        wc = await pool.new_walk_context(
            ContextOptions(allow_private=True, rewrite=page_server.rewrite)
        )
        assert pool.restarts == 1 and pool.browser.is_connected()  # a fresh connection
        await wc.close()
    finally:
        await pool.close()
