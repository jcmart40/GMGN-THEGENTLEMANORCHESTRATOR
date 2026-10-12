"""Dev-following signal source (2026-09-15) -- third piece of the
CopyTrade toolkit integration, GMGN-bot ("the Gentleman"), built AHEAD of
finishing Token Snipe at the user's explicit request.

ARCHITECTURE: same as bot/wallet_follow.py -- this is NOT a separate
trader with its own rules, it's a SIGNAL SOURCE only, feeding candidates
through bot/screener.py's existing `_autonomous_buy()`, same risk gates,
same paper/live execution, same exit management. See wallet_follow.py's
own module docstring for the full "one unified engine" reasoning (user's
explicit call: "I really hate separating logics in to different buckets").

WHY THIS EXISTS SEPARATELY FROM TOKEN SNIPE: mid-build on Token Snipe (which
only ever watches specific token addresses it's given), the user asked a
real question: "what if the token developer launches different tokens not
just this one is he going to snipe the other ones also" -- Token Snipe
can't do that by design. This module can: it watches a DEV WALLET, not a
token, and fires a buy signal on every future token that wallet deploys.

MECHANISM, verified live 2026-09-15 against two real, never-Tracked dev
wallets before writing this file (gmgn-cli's own installed source +
skills/gmgn-portfolio/SKILL.md, not guessed):
  - UNLIKE `track follow-wallet` (bot/wallet_follow.py's mechanism), this
    does NOT require the target wallet be Tracked via GMGN's platform UI
    first. `portfolio created-tokens` and `portfolio activity` both use
    lighter "exist auth" (API key only) rather than the signed/
    account-scoped "critical auth" that caused wallet-follow's Track-first
    requirement. Confirmed by requesting real data for two different,
    never-Tracked wallets and getting back correct, wallet-specific
    results each time (matching wallet field, matching known token
    creations) -- not a fallback to some unrelated wallet's data.
  - Buy signal: `portfolio created-tokens` per watched dev. A fresh
    `create_timestamp` beyond the last-seen baseline = a brand-new
    deployment. The array returns newest-first (confirmed live) -- so even
    though the docs warn it's capped at 100 entries and may be truncated,
    truncation can only ever drop the OLDEST entries for a prolific dev,
    never the newest. A fresh launch can never be silently missed this way.
  - Sell signal: `portfolio activity --type sell` per watched dev. Mirrors
    the exit for ANY sell of a token we hold a dev_snipe-sourced position
    in. Deliberately does NOT try to read `is_open_or_close` (full vs
    partial close) here -- unlike follow-wallet, gmgn-cli's own docs do not
    document that field's meaning for `portfolio activity` specifically,
    so guessing at it would be exactly the kind of unverified assumption
    this whole build has deliberately avoided. Every sell is treated as a
    real signal.

Multiple devs are supported from day one via a watchlist file (JSON array
of addresses), unlike wallet-follow's single hardcoded pilot wallet --
mirrors Token Snipe's own watchlist-file design. The user adds dev
addresses directly in chat (NOT through the AI's own free-text parsing --
exact wallet addresses are precise data entry, not a strategy judgment
call, same established precedent as Token Snipe/Buzz's own free-text
parsing decision).
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

_SOURCE = "dev_snipe"


def _safe_float(value) -> float | None:
    """created-tokens/activity fields sometimes come back as decimal
    strings, sometimes as numbers, sometimes null/empty for an unindexed
    token -- same defensive pattern already used in bot/security.py's
    check_arbitrary_token_candidate() for the identical reason."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_watchlist() -> list[str]:
    """JSON array of dev wallet addresses, user-populated directly (see
    module docstring). Missing/invalid file just means nothing is watched
    yet -- never crash the loop over it."""
    if not os.path.exists(config.dev_snipe_watchlist_file):
        return []
    try:
        with open(config.dev_snipe_watchlist_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            # Dedupe while preserving order -- a duplicate entry would
            # otherwise cost a second round trip (created-tokens weight 2 +
            # activity weight 3) against the shared rate-limit budget for
            # zero benefit, since it's the same dev polled twice per cycle.
            seen: set[str] = set()
            wallets = []
            for addr in data:
                addr = str(addr) if addr else ""
                if addr and addr not in seen:
                    seen.add(addr)
                    wallets.append(addr)
            return wallets
        log.warning("dev_snipe: watchlist file is not a JSON array -- ignoring it this cycle.")
        return []
    except Exception:
        log.exception("dev_snipe: watchlist file unreadable -- treating as empty this cycle.")
        return []


def _load_state() -> dict:
    """Keyed by dev wallet address -> {"last_seen_create_ts", "last_seen_activity_ts"}.
    None (not 0.0) on either field signals "never polled this dev before" --
    same first-run-burst bug avoidance as bot/wallet_follow.py's own
    _load_state().

    Cross-process-locked on its own (2026-10 latency audit) -- the lock used
    to wrap the entire _poll_once() call in run_forever(), which meant it
    was held across every network `await` in that cycle (one per watched
    dev wallet), blocking the asyncio event loop for however long those
    round trips took. The lock only needs to cover this file's own
    synchronous read, so it now lives here instead."""
    if not os.path.exists(config.dev_snipe_state_file):
        return {}
    try:
        with file_lock.cross_process_lock(config.dev_snipe_state_lock_file):
            with open(config.dev_snipe_state_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        log.exception("dev_snipe state file unreadable -- starting fresh rather than blocking the loop.")
        return {}


def _save_state(state: dict) -> None:
    directory = os.path.dirname(config.dev_snipe_state_file) or "."
    os.makedirs(directory, exist_ok=True)
    with file_lock.cross_process_lock(config.dev_snipe_state_lock_file):
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, config.dev_snipe_state_file)


def _has_dev_snipe_position(address: str, mode: str) -> bool:
    """Same reasoning as wallet_follow.py's own _has_wallet_follow_position --
    only mirror an exit for a position this signal source itself opened,
    never a screening-sourced position that happens to share the token."""
    positions = (paper_trader.get_portfolio() if mode == "paper" else executor.get_positions())["positions"]
    pos = positions.get(address)
    return pos is not None and pos.get("source") == _SOURCE


async def _handle_new_token_signal(token: dict, dev_address: str) -> None:
    address = token.get("token_address")
    symbol = token.get("symbol")
    if not address:
        return

    # Signal-to-buy latency instrumentation (2026-09-16) -- see
    # bot/screener.py's own screen_once() for the fuller reasoning. This is
    # the earliest point in code where this module has actually seen the
    # signal (the polling loop that calls this function has no further
    # detection step of its own), so it's the truest "trigger" moment
    # available here.
    trigger_ts = time.time()

    try:
        # security.check_arbitrary_token_candidate() is async and does its
        # own internal thread-offloading + shared rate_limiter reporting
        # (2026-09-15 audit fix) -- no asyncio.to_thread() wrapper needed
        # here anymore.
        result = await security.check_arbitrary_token_candidate(address, config.chain)
    except gmgn.GmgnCliError:
        log.exception("dev_snipe: security check failed for %s (%s) -- skipping this signal.", symbol, address)
        return

    if not result.passed:
        log.info("dev_snipe: %s (%s) [dev %s] REJECTED -- %s", symbol, address, dev_address, "; ".join(result.reasons))
        return

    candidate = {
        "address": address,
        "symbol": symbol,
        "name": symbol,
        # created-tokens already ships market_cap/pool_liquidity directly --
        # no extra get_token_info() round trip needed, unlike wallet_follow's
        # buy path (that signal only ever carries a bare trade record).
        "market_cap": _safe_float(token.get("market_cap")),
        "liquidity": _safe_float(token.get("pool_liquidity")),
        "trigger_ts": trigger_ts,
    }
    empty_trenches = security.SecurityResult(passed=True, reasons=[])

    log.warning("[bold cyan]DEV-SNIPE SIGNAL[/bold cyan] %s (%s) -- dev %s deployed a new token, "
                "buying via the Gentleman's own buy path.", symbol, address, dev_address)

    # Lazy import -- same circular-import reasoning as wallet_follow.py:
    # this module is imported BY screener.py's main() below.
    import bot.screener as screener
    await screener._autonomous_buy(candidate, empty_trenches, result, source=_SOURCE)


async def _handle_dev_sell_signal(trade: dict, dev_address: str) -> None:
    token = trade.get("token") or {}
    address = token.get("address")
    symbol = token.get("symbol")
    if not address:
        return

    mode = trading_control.get_mode()
    if mode not in ("paper", "live"):
        return
    if not _has_dev_snipe_position(address, mode):
        return

    log.warning("[bold cyan]DEV-SNIPE EXIT SIGNAL[/bold cyan] %s (%s) -- dev %s sold, "
                "mirroring the exit.", symbol, address, dev_address)
    try:
        if mode == "paper":
            await asyncio.to_thread(paper_trader.simulate_sell, address, "dev_snipe_exit")
        else:
            await asyncio.to_thread(executor.execute_sell, address, "dev_snipe_exit")
    except Exception:
        log.exception("dev_snipe: mirror-sell failed for %s (%s) -- position stays open, normal "
                       "exit management (stop-loss/take-profit/stale) still protects it.", symbol, address)


async def _poll_wallet_once(dev_address: str, dev_state: dict) -> dict:
    """Polls one watched dev's created-tokens (buy side) and sell activity
    (exit side), returns the updated per-dev state dict. Never raises --
    a rate-limit or API error on one dev must not stop the others in the
    same cycle (caller loops over the whole watchlist)."""
    try:
        created = await asyncio.to_thread(gmgn.get_wallet_created_tokens, dev_address, config.chain)
        rate_limiter.reset()
    except gmgn.GmgnRateLimitError as exc:
        # Shared backoff (2026-09-15 audit fix) -- see wallet_follow.py's
        # own comment on this same fix for the full reasoning: this and
        # the other 4 signal sources built the same day bypassed the
        # shared circuit breaker entirely before this, which is exactly
        # the failure mode bot/rate_limiter.py exists to prevent.
        log.warning("dev_snipe: rate limited fetching created-tokens for %s -- backing off.", dev_address)
        await rate_limiter.handle_hit(exc.retry_after_secs)
        return dev_state
    except gmgn.GmgnCliError:
        log.exception("dev_snipe: created-tokens fetch failed for %s -- skipping this dev this cycle.", dev_address)
        return dev_state
    tokens = created.get("tokens", []) if isinstance(created, dict) else []

    try:
        activities = await asyncio.to_thread(gmgn.get_wallet_activity, dev_address, config.chain, types=["sell"], limit=20)
        rate_limiter.reset()
    except gmgn.GmgnRateLimitError as exc:
        log.warning("dev_snipe: rate limited fetching activity for %s -- backing off (buy-side still processed this cycle).", dev_address)
        await rate_limiter.handle_hit(exc.retry_after_secs)
        activities = []
    except gmgn.GmgnCliError:
        log.exception("dev_snipe: activity fetch failed for %s -- buy-side still processed this cycle.", dev_address)
        activities = []
    # Paranoia filter, same reasoning as wallet_follow.py's own maker check --
    # never let a bad response silently mix in another wallet's activity.
    activities = [a for a in activities if a.get("wallet") == dev_address]

    last_create_ts = dev_state.get("last_seen_create_ts")
    last_activity_ts = dev_state.get("last_seen_activity_ts")

    if last_create_ts is None or last_activity_ts is None:
        # First poll ever for this dev -- seed both baselines WITHOUT
        # acting on anything already in either batch, same reasoning as
        # wallet_follow.py's own first-run bug fix.
        newest_create = max((t.get("create_timestamp", 0) for t in tokens), default=time.time())
        newest_activity = max((a.get("timestamp", 0) for a in activities), default=time.time())
        log.info("dev_snipe: first poll for %s, baselines seeded (create=%s, activity=%s) -- "
                  "watching for new activity from here.", dev_address, newest_create, newest_activity)
        return {"last_seen_create_ts": newest_create, "last_seen_activity_ts": newest_activity}

    new_tokens = [t for t in tokens if t.get("create_timestamp", 0) > last_create_ts]
    new_tokens.sort(key=lambda t: t.get("create_timestamp", 0))
    newest_create_ts = last_create_ts
    for token in new_tokens:
        ts = token.get("create_timestamp", 0)
        if ts > newest_create_ts:
            newest_create_ts = ts
        try:
            await _handle_new_token_signal(token, dev_address)
        except Exception:
            log.exception("dev_snipe: buy-signal handling crashed for %s -- continuing with the rest of this batch.",
                           token.get("symbol"))

    new_sells = [a for a in activities if a.get("timestamp", 0) > last_activity_ts]
    new_sells.sort(key=lambda a: a.get("timestamp", 0))
    newest_activity_ts = last_activity_ts
    for trade in new_sells:
        ts = trade.get("timestamp", 0)
        if ts > newest_activity_ts:
            newest_activity_ts = ts
        try:
            await _handle_dev_sell_signal(trade, dev_address)
        except Exception:
            log.exception("dev_snipe: sell-signal handling crashed for %s -- continuing with the rest of this batch.",
                           (trade.get("token") or {}).get("symbol"))

    return {"last_seen_create_ts": newest_create_ts, "last_seen_activity_ts": newest_activity_ts}


async def _poll_once() -> None:
    wallets = _load_watchlist()
    if not wallets:
        return  # nothing to watch yet -- user hasn't added a dev address

    state = _load_state()
    for dev_address in wallets:
        dev_state = state.get(dev_address, {"last_seen_create_ts": None, "last_seen_activity_ts": None})
        try:
            state[dev_address] = await _poll_wallet_once(dev_address, dev_state)
        except Exception:
            # _poll_wallet_once already catches its own per-token/per-trade
            # errors internally -- this is the outer safety net for
            # anything else (a bug in this module itself), so one bad dev
            # can never block the rest of the watchlist from being polled
            # this cycle. Leaves that dev's state untouched -- retried next
            # cycle rather than silently advancing its baseline past
            # whatever caused the crash.
            log.exception("dev_snipe: poll crashed for dev %s -- continuing with the rest of the watchlist.", dev_address)
    _save_state(state)


async def run_forever() -> None:
    if not config.dev_snipe_enabled:
        log.info("dev_snipe: disabled (DEV_SNIPE_ENABLED=0), loop not starting.")
        return
    log.warning("dev_snipe: watching %d dev wallet(s) every %.0fs.",
                len(_load_watchlist()), config.dev_snipe_poll_interval_secs)
    while True:
        try:
            # Locking now happens inside _load_state()/_save_state()
            # themselves (2026-10 latency audit) -- not held here across
            # _poll_once()'s network awaits, so a slow dev-wallet round trip
            # can no longer block tui.py or any other process waiting on
            # this same lock file.
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad cycle must never kill the whole loop
            log.exception("dev_snipe: poll cycle crashed -- continuing on the next one.")
        await asyncio.sleep(config.dev_snipe_poll_interval_secs)
