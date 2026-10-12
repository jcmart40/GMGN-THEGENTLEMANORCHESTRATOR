"""Shared GMGN rate-limit backoff state -- used by BOTH bot/screener.py
(trenches poll, token_security checks, live buys) and bot/exit_manager.py
(position price checks). Extracted into its own module 2026-09-06 rather
than left as screener.py-private functions: it's the same actual GMGN
account/IP-level ban affecting both loops, so they need to share ONE
consecutive-hit counter, not track it independently -- two separate
counters would each think they were only hitting occasional isolated
rate limits when together they might be hammering GMGN constantly.

Real incident this exists to prevent: a ~300s ban that should have
cleared kept resetting to a fresh ~300s on every bot restart, because the
backoff only waited a few seconds (a generic exponential schedule) before
retrying, and GMGN's own words are explicit that repeated requests during
a ban EXTEND it. Fixed by honoring GMGN's own reported wait time
(retry_after_secs, parsed in bot/gmgn_client.py's GmgnRateLimitError) when
available, falling back to the exponential/circuit-breaker schedule only
when GMGN's message didn't include a parseable one.
"""
from __future__ import annotations

import asyncio
import logging
import random

from config import config

log = logging.getLogger("gmgn-agent")

_state = {"consecutive": 0}


async def handle_hit(retry_after_secs: float | None = None) -> None:
    _state["consecutive"] += 1
    n = _state["consecutive"]

    if retry_after_secs is not None:
        wait = retry_after_secs + 5.0  # safety margin past GMGN's own reported reset
        log.warning("[bold red]RATE LIMIT[/bold red] GMGN reports %.0fs remaining -- waiting %.0fs "
                    "(consecutive hit #%d) rather than guessing short and extending the ban further.",
                    retry_after_secs, wait, n)
        await asyncio.sleep(wait)
        _state["consecutive"] = 0
        return

    if n >= config.rate_limit_max_consecutive:
        log.warning("[bold red]RATE LIMIT CIRCUIT BREAKER[/bold red] %d consecutive hits -- "
                    "pausing %.0fs before resuming (GMGN's own ban can extend with continued requests).",
                    n, config.rate_limit_cooldown_secs)
        await asyncio.sleep(config.rate_limit_cooldown_secs)
        _state["consecutive"] = 0
    else:
        delay = config.rate_limit_backoff_base_secs * (2 ** (n - 1)) + random.uniform(0, 1.0)
        log.warning("Rate limit hit (%d/%d consecutive) -- backing off %.1fs.",
                    n, config.rate_limit_max_consecutive, delay)
        await asyncio.sleep(delay)


def reset() -> None:
    _state["consecutive"] = 0
