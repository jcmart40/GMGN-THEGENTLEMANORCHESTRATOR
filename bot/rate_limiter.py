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
import time

from config import config

log = logging.getLogger("gmgn-agent")

_state = {"consecutive": 0, "throttled_until": 0.0}


def throttled() -> bool:
    """True while a rate-limit cooldown is still in force (set by
    handle_hit()'s wait and cleared once it elapses). Added 2026-10-17 so the
    screening loop can short-circuit an entire pass instead of iterating
    hundreds of candidates that would each fail their security check (and
    each risk a 15s-spinning gmgn-cli subprocess) while an active ban is in
    force. Import: `import time` at module top."""
    return time.monotonic() < _state["throttled_until"]


async def handle_hit(retry_after_secs: float | None = None) -> None:
    _state["consecutive"] += 1
    n = _state["consecutive"]

    if retry_after_secs is not None:
        # Wait at least the full cooldown even when GMGN (or the CLI) reports
        # a shorter reset. GMGN's own 429 text is explicit that repeated
        # requests during a ban EXTEND it ("by 5s up to 5 minutes"), so the
        # safe move is a single hard pause that outlasts the worst case rather
        # than probing again the moment a reset is claimed and re-triggering a
        # longer ban. Raised 2026-10-17 after the 10/15 incident.
        wait = max(retry_after_secs + 5.0, config.rate_limit_cooldown_secs)
        log.warning("[bold red]RATE LIMIT[/bold red] GMGN reports %.0fs remaining -- waiting %.0fs "
                    "(cooldown floor) (consecutive hit #%d); requests during a ban extend it, "
                    "so we wait the full window before probing again.",
                    retry_after_secs, wait, n)
        _state["throttled_until"] = time.monotonic() + wait
        await asyncio.sleep(wait)
        _state["consecutive"] = 0
        return

    if n >= config.rate_limit_max_consecutive:
        log.warning("[bold red]RATE LIMIT CIRCUIT BREAKER[/bold red] %d consecutive hits -- "
                    "pausing %.0fs before resuming (GMGN's own ban can extend with continued requests).",
                    n, config.rate_limit_cooldown_secs)
        _state["throttled_until"] = time.monotonic() + config.rate_limit_cooldown_secs
        await asyncio.sleep(config.rate_limit_cooldown_secs)
        _state["consecutive"] = 0
    else:
        delay = config.rate_limit_backoff_base_secs * (2 ** (n - 1)) + random.uniform(0, 1.0)
        log.warning("Rate limit hit (%d/%d consecutive) -- backing off %.1fs.",
                    n, config.rate_limit_max_consecutive, delay)
        _state["throttled_until"] = time.monotonic() + delay
        await asyncio.sleep(delay)


def reset() -> None:
    _state["consecutive"] = 0
    _state["throttled_until"] = 0.0
