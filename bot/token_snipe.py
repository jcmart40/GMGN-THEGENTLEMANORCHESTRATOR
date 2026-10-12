"""Token Snipe signal source (2026-09-15) -- second piece of the CopyTrade
toolkit integration, GMGN-bot ("the Gentleman"), config scoped the same day
as wallet-follow but paused mid-build (twice -- once for Dev Snipe, once
for SnipeX) before finally being finished.

ARCHITECTURE: same as every other piece -- this is NOT a separate trader
with its own rules, it's a SIGNAL SOURCE only, feeding candidates through
bot/screener.py's existing `_autonomous_buy()`, same risk gates, same
paper/live execution, same exit management. See bot/wallet_follow.py's own
module docstring for the full "one unified engine" reasoning. No sell-side
mirror here either (like bot/snipex.py) -- exits are handled entirely by
the shared bot/exit_manager.py, same as every position regardless of
source.

WHAT THIS WATCHES: specific, known token addresses the user gives directly
in chat -- NOT through the AI's own free-text parsing (exact contract
addresses are precise data entry, not a strategy judgment call, same
established precedent as every other watchlist in this project). Claude
adds each address straight to the watchlist file.

ONE unified watch-loop design covers BOTH real scenarios the user actually
described, not two separate mechanisms:
  - A genuinely pre-launch token: not enough liquidity yet, keeps getting
    rejected by the real security check every poll cycle until it
    actually qualifies (or never does -- no special-cased permanent
    rejection, see the state-file's own comment in config.py for why).
  - An already-migrated token with real volume (the user's own real
    example: "he can still do it even if it already migrated to that kind
    of volume" -- given after testing against 6GmAFSYs4gk3FDao5Fzzy
    SQpPZaWsa4rUJHacpMpUNgx/STONK, an already-liquid token with $846,741
    liquidity at the time): the very first poll already passes the real
    check, so it fires almost immediately -- no separate "is this
    pre-launch or already-live" branch needed, the SAME check just
    naturally passes right away for a token that already qualifies.

Once an address passes the real security check and a buy fires, its state
is marked "bought" so it's never re-bought on a later poll -- watching
continues for every OTHER still-pending address on the list, indefinitely,
until the user removes an entry themselves.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time

import bot.file_lock as file_lock
import bot.gmgn_client as gmgn
import bot.security as security
from config import config

log = logging.getLogger("gmgn-agent")

_SOURCE = "token_snipe"


def _load_watchlist() -> list[str]:
    """JSON array of token addresses, user-populated directly (see module
    docstring). Missing/invalid file just means nothing is watched yet."""
    if not os.path.exists(config.token_snipe_watchlist_file):
        return []
    try:
        with open(config.token_snipe_watchlist_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            log.warning("token_snipe: watchlist file is not a JSON array -- ignoring it this cycle.")
            return []
        seen: set[str] = set()
        addresses = []
        for a in data:
            a = str(a).strip() if a else ""
            if a and a not in seen:
                seen.add(a)
                addresses.append(a)
        return addresses
    except Exception:
        log.exception("token_snipe: watchlist file unreadable -- treating as empty this cycle.")
        return []


def _load_state() -> dict:
    """Keyed by address -> {"status": "watching"|"bought"}. Missing entry
    for a watchlist address defaults to "watching" -- see _poll_once().

    Cross-process-locked on its own (2026-10 latency audit) -- the lock used
    to wrap the entire _poll_once() call in run_forever(), which meant it
    was held across every network `await` in that cycle (one per watched
    address), blocking the asyncio event loop for however long those round
    trips took. The lock only needs to cover this file's own synchronous
    read, so it now lives here instead."""
    if not os.path.exists(config.token_snipe_state_file):
        return {}
    try:
        with file_lock.cross_process_lock(config.token_snipe_state_lock_file):
            with open(config.token_snipe_state_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        log.exception("token_snipe state file unreadable -- starting fresh rather than blocking the loop.")
        return {}


def _save_state(state: dict) -> None:
    directory = os.path.dirname(config.token_snipe_state_file) or "."
    os.makedirs(directory, exist_ok=True)
    with file_lock.cross_process_lock(config.token_snipe_state_lock_file):
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, config.token_snipe_state_file)


async def _try_buy(address: str) -> bool:
    """Returns True if a buy signal fired (address should be marked
    bought), False if it was rejected this cycle (stays "watching",
    retried next poll)."""
    # Signal-to-buy latency instrumentation (2026-09-16) -- see
    # bot/screener.py's own screen_once() for the fuller reasoning. Stamped
    # here, the earliest point in this module's own poll cycle where a
    # candidate address is actually being evaluated.
    trigger_ts = time.time()
    try:
        # security.check_arbitrary_token_candidate() is async and does its
        # own internal thread-offloading + shared rate_limiter reporting
        # (2026-09-15 audit fix) -- no asyncio.to_thread() wrapper needed
        # here anymore.
        result = await security.check_arbitrary_token_candidate(address, config.chain)
    except gmgn.GmgnCliError:
        log.exception("token_snipe: security check failed for %s -- will retry next cycle.", address)
        return False

    if not result.passed:
        log.info("token_snipe: %s not ready yet -- %s", address, "; ".join(result.reasons))
        return False

    # Reuse the info the security check already fetched (2026-09-15 speed
    # fix) -- no more redundant third get_token_info() call for data
    # that's already sitting right here on the result.
    info = result.info or {}
    symbol = info.get("symbol") or address[:8]
    price = (info.get("price") or {}).get("price")
    market_cap = None
    if price:
        try:
            supply = info.get("total_supply")
            if supply is not None:
                market_cap = float(price) * float(supply)
        except (TypeError, ValueError):
            market_cap = None

    candidate = {
        "address": address,
        "symbol": symbol,
        "name": symbol,
        "market_cap": market_cap,
        "liquidity": info.get("liquidity"),
        "trigger_ts": trigger_ts,
    }
    empty_trenches = security.SecurityResult(passed=True, reasons=[])

    log.warning("[bold cyan]TOKEN-SNIPE SIGNAL[/bold cyan] %s (%s) qualified -- buying via the "
                "Gentleman's own buy path.", symbol, address)

    # Lazy import -- same circular-import reasoning as every other signal
    # source here.
    import bot.screener as screener
    await screener._autonomous_buy(candidate, empty_trenches, result, source=_SOURCE)
    return True


async def _poll_once() -> None:
    addresses = _load_watchlist()
    if not addresses:
        return  # nothing to watch yet

    state = _load_state()
    for address in addresses:
        entry = state.get(address, {"status": "watching"})
        if entry.get("status") == "bought":
            continue  # already fired -- keep watching everything else
        try:
            bought = await _try_buy(address)
        except Exception:
            log.exception("token_snipe: poll crashed for %s -- continuing with the rest of the watchlist.", address)
            continue
        state[address] = {"status": "bought" if bought else "watching"}
    _save_state(state)


async def run_forever() -> None:
    if not config.token_snipe_enabled:
        log.info("token_snipe: disabled (TOKEN_SNIPE_ENABLED=0), loop not starting.")
        return
    log.warning("token_snipe: watching %d token(s) every %.0fs.",
                len(_load_watchlist()), config.token_snipe_poll_interval_secs)
    while True:
        try:
            # Locking now happens inside _load_state()/_save_state()
            # themselves (2026-10 latency audit) -- not held here across
            # _poll_once()'s network awaits, so a slow candidate check can
            # no longer block tui.py or any other process waiting on this
            # same lock file.
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad cycle must never kill the whole loop
            log.exception("token_snipe: poll cycle crashed -- continuing on the next one.")
        await asyncio.sleep(config.token_snipe_poll_interval_secs)
