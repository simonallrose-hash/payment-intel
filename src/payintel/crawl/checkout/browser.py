"""Chromium through Playwright (FR-CW-01, FR-CW-10): isolated context per walk, no stealth.

- one browser process per worker, restarted every N walks (default 50);
- a fresh `BrowserContext` per domain: no cookies or storage shared between
  walks; identifying User-Agent (LR-02), locale and Accept-Language of the
  target country (FR-CW-11), HAR recording without response bodies (FR-CW-08);
- the guardrail init script and request router are installed on every
  context before the first page opens (FR-CW-04);
- memory is read through CDP `Performance.getMetrics` (JS heap of the page);
  the walk aborts with `memory_limit` when it exceeds the configured limit.

No fingerprint is altered: headless or headed is a plain launch flag (AS-20).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from payintel.crawl.checkout.dictionary import CheckoutDictionary
from payintel.crawl.checkout.guardrails.injection import (
    Rewriter,
    RouteStats,
    init_script,
    route_handler,
)

ENV_EXECUTABLE = "PAYINTEL_CHROMIUM_EXECUTABLE"


@dataclass
class ContextOptions:
    locale: str = "en-GB"
    accept_language: str = "en-GB,en;q=0.8"
    har_path: Path | None = None
    allow_private: bool = False
    rewrite: Rewriter | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    proxy: dict[str, str] | None = None  # FR-CW-11: geolocation proxy for this context only


def context_kwargs(
    opts: ContextOptions, *, user_agent: str, viewport: tuple[int, int]
) -> dict[str, Any]:
    """Playwright `new_context` arguments: identifying UA (LR-02), locale/Accept-Language of
    the walk's country (FR-CW-11), optional geolocation proxy, no downloads/service workers."""
    kwargs: dict[str, Any] = {
        "user_agent": user_agent,
        "locale": opts.locale,
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "extra_http_headers": {"Accept-Language": opts.accept_language, **opts.extra_headers},
        "ignore_https_errors": False,
        "java_script_enabled": True,
        "accept_downloads": False,
        "service_workers": "block",
    }
    if opts.proxy:
        kwargs["proxy"] = dict(opts.proxy)
    return kwargs


@dataclass
class WalkContext:
    """One isolated browser context plus its route statistics."""

    context: BrowserContext
    stats: RouteStats
    har_path: Path | None
    allow_private: bool = False
    rewrite: Rewriter | None = None
    on_close: Callable[[], Awaitable[None]] | None = None
    _closed: bool = False

    async def new_page(self) -> Page:
        return await self.context.new_page()

    async def cookie_names(self) -> list[str]:
        try:
            return sorted({c["name"] for c in await self.context.cookies()})
        except PlaywrightError:
            return []

    async def close(self) -> bytes | None:
        """Close the context; returns the HAR bytes when recording was on."""
        if self._closed:
            return None
        self._closed = True
        try:
            await self.context.close()
        except PlaywrightError:
            return None
        finally:
            if self.on_close is not None:
                await self.on_close()
        if self.har_path is not None and self.har_path.exists():
            data = self.har_path.read_bytes()
            with contextlib.suppress(OSError):
                self.har_path.unlink()
            return data
        return None


class BrowserPool:
    def __init__(
        self,
        dictionary: CheckoutDictionary,
        *,
        user_agent: str,
        headless: bool = True,
        restart_every: int = 50,
        viewport: tuple[int, int] = (1366, 900),
        executable_path: str | None = None,
        har_dir: Path | None = None,
        on_restart: Callable[[], None] | None = None,
    ) -> None:
        self.dictionary = dictionary
        self.user_agent = user_agent
        self.headless = headless
        self.restart_every = max(1, restart_every)
        self.viewport = viewport
        self.executable_path = executable_path or os.environ.get(ENV_EXECUTABLE) or None
        self.har_dir = har_dir
        self.on_restart = on_restart
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._init_script = init_script(dictionary)
        self.walks_since_restart = 0
        self.restarts = 0
        self.active_contexts = 0
        self._cond: asyncio.Condition | None = None

    async def __aenter__(self) -> BrowserPool:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._pw is None:
            self._pw = await async_playwright().start()
        await self._launch()

    async def _launch(self) -> None:
        if self._pw is None:
            raise RuntimeError("Playwright driver is not started")
        kwargs: dict[str, Any] = {"headless": self.headless}
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        self._browser = await self._pw.chromium.launch(**kwargs)
        self.walks_since_restart = 0

    async def restart(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(PlaywrightError):
                await self._browser.close()
        await self._launch()
        self.restarts += 1
        if self.on_restart:
            self.on_restart()

    async def close(self) -> None:
        if self._browser is not None:
            with contextlib.suppress(PlaywrightError):
                await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            raise RuntimeError("BrowserPool is not started")
        return self._browser

    @property
    def version(self) -> str:
        return self.browser.version

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def _release(self) -> None:
        async with self._condition():
            self.active_contexts = max(0, self.active_contexts - 1)
            self._condition().notify_all()

    async def new_walk_context(self, opts: ContextOptions) -> WalkContext:
        """A fresh isolated context (FR-CW-01); restarts the browser every `restart_every` walks.

        A restart closes every context of the browser, so it waits until the walks
        in flight have closed theirs (new walks queue behind the restart meanwhile).
        """
        cond = self._condition()
        async with cond:
            if self.walks_since_restart >= self.restart_every or not self.browser.is_connected():
                while self.active_contexts > 0 and self.browser.is_connected():
                    await cond.wait()
                await self.restart()
            self.walks_since_restart += 1
            self.active_contexts += 1
        try:
            return await self._new_context(opts)
        except BaseException:
            await self._release()
            raise

    async def _new_context(self, opts: ContextOptions) -> WalkContext:
        har_path = opts.har_path
        if har_path is None and self.har_dir is not None:
            fd, name = tempfile.mkstemp(prefix="walk-", suffix=".har", dir=self.har_dir)
            os.close(fd)
            har_path = Path(name)
        kwargs = context_kwargs(opts, user_agent=self.user_agent, viewport=self.viewport)
        if har_path is not None:
            kwargs["record_har_path"] = str(har_path)
            kwargs["record_har_content"] = "omit"
        context = await self.browser.new_context(**kwargs)
        stats = RouteStats()
        await context.add_init_script(self._init_script)
        await context.route(
            "**/*",
            route_handler(
                self.dictionary, stats, allow_private=opts.allow_private, rewrite=opts.rewrite
            ),
        )
        return WalkContext(
            context=context,
            stats=stats,
            har_path=har_path,
            allow_private=opts.allow_private,
            rewrite=opts.rewrite,
            on_close=self._release,
        )

    @staticmethod
    async def js_heap_mb(page: Page) -> float:
        """JS heap of the page's renderer via CDP; 0 when unavailable."""
        try:
            cdp = await page.context.new_cdp_session(page)
            try:
                metrics = await cdp.send("Performance.getMetrics")
            finally:
                await cdp.detach()
        except PlaywrightError:
            return 0.0
        for m in metrics.get("metrics", []):
            if m.get("name") == "JSHeapUsedSize":
                return float(m.get("value", 0)) / (1024 * 1024)
        return 0.0
