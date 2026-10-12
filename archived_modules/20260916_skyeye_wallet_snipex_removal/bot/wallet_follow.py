"""Wallet-following signal source (2026-09-15) -- first piece of the
CopyTrade toolkit integration, GMGN-bot ("the Gentleman").

ARCHITECTURE, read this before touching anything here: this module is NOT
a separate trader with its own rules. It is a SIGNAL SOURCE only -- it
watches a followed wallet's trades and, when a real signal fires, hands a
candidate off through bot/screener.py's existing `_autonomous_buy()`, the
exact same entry point trenches screening already uses. Same risk gates
(mode/time-pause/review-pause/burst-cooldown/min-interval/max-concurrent-
positions), same paper/live execution, same exit management afterward. The
Gentleman himself is the one unified engine; this module just gives him
another way to notice an opportunity. User's explicit, deliberate
architecture call 2026-09-15, after initially scoping (and rejecting) a
"separate bucket with its own AI Free Will loop" design:
    "I really hate separating logics in to different buckets I like it
    better when there is only one bucket managing all parameters"
    ...
    "yes everything free will loop for all"
Do not reintroduce separate tuning/exit logic for this signal source
without the user explicitly asking for it again.

Pilot wallet is Cented (config.wallet_follow_address). Must be Tracked via
GMGN's own platform "Track" button BEFORE `gmgn-cli track follow-wallet`
returns anything real for it -- confirmed live 2026-09-14/15: the
`--wallet` filter looked completely broken (returned an unrelated trending
wallet's trades regardless of which address was requested, even a
nonexistent one) until the target wallet was actually Tracked on the
platform. Real gmgn-cli documentation (skills/gmgn-track/SKILL.md)
confirms this directly: "the follow list is resolved automatically from
the GMGN user account bound to the API Key."

GMGN tags Cented "wash_trader" alongside "kol"/"arbitrager"/"top_followed".
User's explicit, informed, final call, do not re-raise: "I don't care what
gmgn has on cented I want to use cented."
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time

import bot.executor as executor
import bot.file_lock as file_lock
import bot.gmgn_client as gmgn
import bot.paper_trader as paper_trader
import bot.rate_limiter as rate_limiter
import bot.security as security
import bot.trading_control as trading_control
from config import config

log = logging.getLogger("gmgn-agent")

SOL_MINT = "So11111111111111111111111111111111111111112"

# is_open_or_close semantics for `track follow-wallet` SPECIFICALLY --
# confirmed against real gmgn-cli documentation (skills/gmgn-track/SKILL.md),
# not guessed: 1 = full position open or close, 0 = partial add or reduce.
# OPPOSITE meaning for the `kol`/`smartmoney` sub-commands -- never reuse
# this constant/logic for those without re-checking their own docs.
_FULL_POSITION_EVENT = 1


def _load_state() -> dict:
    """`last_seen_ts: None` (not 0.0) signals "never polled before" --
    _poll_once() uses this to seed the baseline on the very first cycle
    WITHOUT acting on anything, rather than treating every trade in that
    first batch as a fresh signal. Real bug caught in self-review
    2026-09-15 before this ever ran live: 0.0 as the "never polled"
    sentinel would have made the first-ever poll treat the whole batch
    (up to 20 trades, however old) as brand new activity, firing a burst of
    mirror-buys on stale history instead of just starting to watch from
    now."""
    if not os.path.exists(config.wallet_follow_state_file):
        return {"last_seen_ts": None}
    try:
        with open(config.wallet_follow_state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("last_seen_ts", None)
        return state
    except Exception:
        log.exception("wallet_follow state file unreadable -- starting fresh "
                       "rather than blocking the loop.")
        return {"last_seen_ts": None}


def _save_state(state: dict) -> None:
    directory = os.path.dirname(config.wallet_follow_state_file) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, config.wallet_follow_state_file)


def _has_wallet_follow_position(address: str, mode: str) -> bool:
    """True if we currently hold an open position in this token that came
    FROM this signal source specifically -- so a full close from the
    followed wallet only ever mirrors our own wallet-follow-sourced
    positions, never a screening-sourced position that happens to share the
    same token by coincidence."""
    positions = (paper_trader.get_portfolio() if mode == "paper" else executor.get_positions())["positions"]
    pos = positions.get(address)
    return pos is not None and pos.get("source") == "wallet_follow"


async def _handle_buy_signal(trade: dict) -> None:
    address = trade.get("base_address")
    symbol = (trade.get("base_token") or {}).get("symbol")
    if not address:
        return

    try:
        # security.check_arbitrary_token_candidate() is async and does its
        # own internal thread-offloading + shared rate_limiter reporting
        # (2026-09-15 audit fix) -- no asyncio.to_thread() wrapper needed
        # here anymore.
        result = await security.check_arbitrary_token_candidate(address, config.chain)
    except gmgn.GmgnCliError:
        log.exception("wallet_follow: security check failed for %s (%s) -- skipping this signal.", symbol, address)
        return

    if not result.passed:
        log.info("wallet_follow: %s (%s) signal REJECTED -- %s", symbol, address, "; ".join(result.reasons))
        return

    # Reuse the info the security check already fetched (2026-09-15 speed
    # fix) -- no more redundant third get_token_info() call for data
    # that's already sitting right here on the result.
    info = result.info or {}
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
    }
    # No trenches step exists for this signal source -- an empty-reasons
    # placeholder here, same shape screener.py's own SecurityResult uses,
    # purely for the activity-log entry's "trenches_passed" field.
    empty_trenches = security.SecurityResult(passed=True, reasons=[])

    log.warning("[bold cyan]WALLET-FOLLOW SIGNAL[/bold cyan] %s (%s) -- Cented opened a full "
                "position, mirroring via the Gentleman's own buy path.", symbol, address)

    # Import here, not at module top -- screener.py imports bot.exit_manager
    # at call time inside its own main(), same lazy-import pattern already
    # used there, and this module is imported BY screener.py's main() below,
    # so importing screener at module load time here would be circular.
    import bot.screener as screener
    await screener._autonomous_buy(candidate, empty_trenches, result, source="wallet_follow")


async def _handle_sell_signal(trade: dict) -> None:
    address = trade.get("base_address")
    symbol = (trade.get("base_token") or {}).get("symbol")
    if not address:
        return

    mode = trading_control.get_mode()
    if mode not in ("paper", "live"):
        return
    if not _has_wallet_follow_position(address, mode):
        return

    log.warning("[bold cyan]WALLET-FOLLOW EXIT SIGNAL[/bold cyan] %s (%s) -- Cented fully closed, "
                "mirroring the exit.", symbol, address)
    try:
        if mode == "paper":
            await asyncio.to_thread(paper_trader.simulate_sell, address, "wallet_follow_exit")
        else:
            await asyncio.to_thread(executor.execute_sell, address, "wallet_follow_exit")
    except Exception:
        log.exception("wallet_follow: mirror-sell failed for %s (%s) -- position stays open, "
                       "normal exit management (stop-loss/take-profit/stale) still protects it.",
                       symbol, address)


async def _poll_once() -> None:
    state = _load_state()
    last_seen_ts = state["last_seen_ts"]

    try:
        trades = await asyncio.to_thread(
            gmgn.get_follow_wallet_trades,
            chain=config.chain, wallet=config.wallet_follow_address, limit=20,
        )
        rate_limiter.reset()
    except gmgn.GmgnRateLimitError as exc:
        # Shared backoff (2026-09-15 audit fix) -- bot/rate_limiter.py's own
        # docstring exists specifically to prevent multiple concurrent
        # loops against the same GMGN account each thinking they're only
        # hitting occasional isolated limits when together they're
        # hammering it constantly. This signal source (and the other 4
        # built the same day) called plain log-and-skip before this fix,
        # completely outside the shared circuit breaker -- exactly the
        # failure mode that module was built to prevent, and a real risk
        # of extending a ban further per GMGN's own documented behavior.
        log.warning("wallet_follow: hit GMGN's rate limit, backing off before the next cycle.")
        await rate_limiter.handle_hit(exc.retry_after_secs)
        return
    except gmgn.GmgnCliError:
        log.exception("wallet_follow: poll failed -- skipping this cycle.")
        return

    # Sanity-check the data is actually the followed wallet's -- real risk
    # given how fragile this endpoint turned out to be (confirmed live
    # 2026-09-14/15: it silently fell back to an unrelated trending
    # wallet's feed when the target wasn't Tracked on the platform, with no
    # error at all). If the account's tracking ever changes again
    # (accidentally untracked, GMGN changes its fallback), this must never
    # silently start mirroring a stranger instead.
    trades = [t for t in trades if t.get("maker") == config.wallet_follow_address]
    if not trades:
        log.warning("wallet_follow: no trades matched %s in this batch -- if this "
                     "persists, check the wallet is still Tracked on the platform.",
                     config.wallet_follow_address)
        return

    if last_seen_ts is None:
        # First poll ever -- seed the baseline from the newest trade
        # already in this batch WITHOUT acting on any of it (see
        # _load_state()'s own docstring for the bug this avoids).
        newest_ts = max((t.get("timestamp", 0) for t in trades), default=time.time())
        _save_state({"last_seen_ts": newest_ts})
        log.info("wallet_follow: first poll, baseline set to %s -- watching for new activity from here.", newest_ts)
        return

    new_trades = [t for t in trades if t.get("timestamp", 0) > last_seen_ts]
    if not new_trades:
        return
    # API returns newest-first -- reverse so signals are handled in the
    # order they actually happened.
    new_trades.reverse()

    newest_ts = last_seen_ts
    for trade in new_trades:
        ts = trade.get("timestamp", 0)
        if ts > newest_ts:
            newest_ts = ts
        try:
            if trade.get("is_open_or_close") != _FULL_POSITION_EVENT:
                continue  # partial add/reduce -- not a full-conviction signal, skip
            side = trade.get("side")
            if side == "buy":
                if trade.get("quote_address") != SOL_MINT:
                    continue  # not a plain SOL-denominated buy -- can't cleanly mirror, skip
                await _handle_buy_signal(trade)
            elif side == "sell":
                await _handle_sell_signal(trade)
        except Exception:
            # One bad signal must never stop the rest of the batch from
            # being processed, or block the state save below (which would
            # otherwise cause this whole batch to be re-attempted next
            # cycle -- harmless for a real duplicate buy attempt, since
            # execute_buy()/simulate_buy() already reject an
            # already-open position, but still worth avoiding).
            log.exception("wallet_follow: signal handling crashed for %s -- continuing with the rest of this batch.",
                           trade.get("base_token", {}).get("symbol"))

    _save_state({"last_seen_ts": newest_ts})


async def run_forever() -> None:
    if not config.wallet_follow_enabled:
        log.info("wallet_follow: disabled (WALLET_FOLLOW_ENABLED=0), loop not starting.")
        return
    log.warning("wallet_follow: watching %s every %.0fs.",
                config.wallet_follow_address, config.wallet_follow_poll_interval_secs)
    while True:
        try:
            with file_lock.cross_process_lock(config.wallet_follow_state_lock_file):
                await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad cycle must never kill the whole loop
            log.exception("wallet_follow: poll cycle crashed -- continuing on the next one.")
        await asyncio.sleep(config.wallet_follow_poll_interval_secs)
