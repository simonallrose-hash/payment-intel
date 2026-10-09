"""Telegram Bot API channel (FR-AL-04, AS-19): digest messages, injectable transport."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

MAX_TEXT = 3_800  # Telegram caps messages at 4096 characters


def format_digest(events: Sequence[dict[str, Any]], *, title: str) -> str:
    lines = [title, ""]
    for e in events[:60]:
        entity = f" {e['entity']}" if e.get("entity") else ""
        lines.append(f"• {e['domain']}: {e['type']}{entity} ({str(e['detected_at'])[:10]})")
    if len(events) > 60:
        lines.append(f"… and {len(events) - 60} more")
    text = "\n".join(lines)
    return text[:MAX_TEXT]


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        api_base: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._url = f"{api_base.rstrip('/')}/bot{token}/sendMessage"
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def send(self, chat_id: str, text: str) -> None:
        response = self._client.post(
            self._url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok", False):
            raise RuntimeError(f"telegram: {body.get('description', 'unknown error')}")

    def close(self) -> None:
        self._client.close()
