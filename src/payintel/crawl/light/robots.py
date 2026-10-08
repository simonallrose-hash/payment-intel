"""robots.txt handling for the project's User-Agent (FR-LS-01, LR-02).

Semantics: 2xx → parse; 401/403 → everything disallowed (the site clearly
does not want robots); other 4xx (incl. 404) → everything allowed; 5xx,
timeouts and errors → disallowed for this scan (try again next cycle).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.robotparser import RobotFileParser


@dataclass
class RobotsRules:
    status: int | None
    fetched: bool
    allow_all: bool
    disallow_all: bool
    crawl_delay: float | None
    _parser: RobotFileParser | None
    _agent: str

    def allows(self, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all or self._parser is None:
            return True
        return self._parser.can_fetch(self._agent, url)


def parse_robots(body: str | None, status: int | None, *, agent_token: str) -> RobotsRules:
    if status is None:
        return RobotsRules(None, False, False, True, None, None, agent_token)
    if status in (401, 403):
        return RobotsRules(status, True, False, True, None, None, agent_token)
    if 400 <= status < 500:
        return RobotsRules(status, True, True, False, None, None, agent_token)
    if status >= 500 or body is None:
        return RobotsRules(status, True, False, True, None, None, agent_token)
    parser = RobotFileParser()
    parser.parse(body.splitlines())
    delay_raw = parser.crawl_delay(agent_token)
    delay = float(delay_raw) if delay_raw is not None else None
    return RobotsRules(status, True, False, False, delay, parser, agent_token)


def agent_token(user_agent: str) -> str:
    """`PayIntelBot/1.0 (+https://…)` → `PayIntelBot` (the token sites use in robots.txt)."""
    return user_agent.split("/", 1)[0].split(" ", 1)[0]
