"""SkyEyeCopy signal source (2026-09-15) -- fifth and final piece of the
CopyTrade toolkit integration, GMGN-bot ("the Gentleman"), built last
after wallet-follow, Dev Snipe, SnipeX, and Token Snipe.

ARCHITECTURE: same as every other piece -- this is NOT a separate trader
with its own rules, it's a SIGNAL SOURCE only, feeding candidates through
bot/screener.py's existing `_autonomous_buy()`, same risk gates, same
paper/live execution, same exit management. See bot/wallet_follow.py's own
module docstring for the full "one unified engine" reasoning.

WHAT THIS WATCHES: unlike WalletCopy/wallet-follow (ONE wallet you
personally pick), SkyEyeCopy auto-follows GMGN's OWN curated "smart money"
wallet list via `track smartmoney` -- a global, platform-tagged feed of
wallets "with a statistically proven record of profitable trading,
identified by GMGN's algorithm" (real quote, gmgn-cli's own
skills/gmgn-track/SKILL.md). Exist auth only (API key, weight 1) -- no
Track-first requirement, confirmed via that same documentation, and even
cheaper per-call than every other signal source built so far.

Deliberately does NOT use GMGN's own "AI Filter" toggle from the real
SkyEyeCopy product -- that's an opaque, unverifiable proprietary
algorithm ("System Alpha Active... parameters are encrypted to protect
the strategy alpha"), flagged for real skepticism during the original
toolkit scoping. This runs the SAME transparent
security.check_arbitrary_token_candidate() every other signal source
uses instead -- every parameter and decision stays visible and
verifiable in these actual files.

CRITICAL MECHANISM DETAIL, confirmed via real documentation BEFORE writing
this file -- `is_open_or_close` means the OPPOSITE thing here vs.
`follow-wallet` (bot/wallet_follow.py's own endpoint):
    follow-wallet:      1 = full position open/close; 0 = partial add/reduce
    kol / smartmoney:   0 = position opened/added;     1 = position closed/reduced
Getting this backwards would flip buy/sell logic entirely. Neither value
cleanly distinguishes "full" from "partial" for this endpoint specifically
(unlike follow-wallet's clean 1-vs-0 split) -- 0 covers both a fresh open
AND an addition to an existing position, 1 covers both a full close AND a
partial reduce. Both are treated as real signals here: any 0-side-buy
event is a real "smart money is bullish" signal worth considering, any
1-side-sell event is a real "smart money is bearish/taking profit" signal
worth mirroring as an exit -- erring toward acting on an exit signal
rather than trying to guess which reduces are "big enough" to matter.

SELL-SIDE DESIGN, deliberately different from wallet-follow/dev-snipe:
those two mirror exits from the SAME specific wallet/dev that triggered
the original buy. SkyEyeCopy instead treats a qualifying sell from ANY
smart-money wallet (not just the one whose buy triggered our own position)
as a valid exit signal for a position this source opened -- consistent
with the real thing being followed here being "smart money as a category,"
not one individual trader, and matches gmgn-cli's own documented framework
("Smart money selling... exit signal -- evaluate whether to exit").

DEDUP MECHANISM, different from every other piece: real trades can share
the exact same timestamp (confirmed live 2026-09-15 -- one wallet logged
multiple buys of the same token within the same second), which rules out
the simple "last_seen_ts" scalar cursor every other signal source uses --
a timestamp alone can't reliably tell "already processed" apart from
"new" when several trades land on the identical second. Dedup is instead
a rolling set of recently-seen transaction hashes (see
config.skyeye_copy_seen_hashes_limit's own comment for the cap reasoning).

NOTE: the real `track smartmoney` response has no `quote_address`/
`quote_token` field at all (confirmed against a real captured response --
unlike `follow-wallet`'s response, which does), so unlike wallet-follow
this can NOT filter to "SOL-denominated trades only." Not silently
assumed away -- just genuinely not information this endpoint exposes.

Deliberately kept simple at the user's explicit request ("keep simple and
we can always change if I decide ok") -- fires on ANY single qualifying
smart-money buy. Does NOT require GMGN's own documented "cluster signal"
(3+ wallets converging on the same token within ~30 min) before acting,
even though the real docs flag that as a stronger conviction signal. Can
be tightened later if real data shows single-wallet signals are too noisy
-- not built now since it wasn't asked for.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile

import bot.executor as executor
import bot.file_lock as file_lock
import bot.gmgn_client as gmgn
import bot.paper_trader as paper_trader
import bot.rate_limiter as rate_limiter
import bot.security as security
import bot.trading_control as trading_control
from config import config

log = logging.getLogger("gmgn-agent")

_SOURCE = "skyeye_copy"

# kol/smartmoney-specific meaning -- OPPOSITE of follow-wallet's own
# _FULL_POSITION_EVENT constant. Named distinctly (not reused from
# wallet_follow.py) so the two are never confused at a glance.
_OPENED_OR_ADDED = 0
_CLOSED_OR_REDUCED = 1

# Real gotcha found in live testing 2026-09-15: since `track smartmoney`'s
# response has no `quote_address` field (see module docstring), some
# trade records put the settlement/quote asset itself in `base_address`
# -- confirmed live, a real poll returned "buy" signals for WSOL and USDC
# with base_address pointing straight at their own well-known mints.
# Nobody wants the Gentleman buying wrapped SOL with SOL, or USDC as a
# "smart money gem call." An explicit hard exclusion, same pattern as
# security.py's own meteora_virtual_curve exclusion, rather than trusting
# the general liquidity/holder-concentration thresholds to naturally
# reject these -- a massively liquid, centrally-issued asset could
# plausibly pass those checks on its own merits, so this can't be left
# to chance. Addresses verified live via `gmgn-cli market search`, not
# guessed from memory.
_EXCLUDED_BASE_ADDRESSES = {
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def _load_state() -> dict:
    if not os.path.exists(config.skyeye_copy_state_file):
        return {"initialized": False, "seen_tx_hashes": []}
    try:
        with open(config.skyeye_copy_state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("initialized", False)
        state.setdefault("seen_tx_hashes", [])
        return state
    except Exception:
        log.exception("skyeye_copy state file unreadable -- starting fresh rather than blocking the loop.")
        return {"initialized": False, "seen_tx_hashes": []}


def _save_state(state: dict) -> None:
    # Trim to the most recent N -- see config.skyeye_copy_seen_hashes_limit's
    # own comment for why. list order here is insertion order (oldest
    # first), so a simple tail-slice keeps the newest entries.
    hashes = state.get("seen_tx_hashes", [])
    if len(hashes) > config.skyeye_copy_seen_hashes_limit:
        state["seen_tx_hashes"] = hashes[-config.skyeye_copy_seen_hashes_limit:]

    directory = os.path.dirname(config.skyeye_copy_state_file) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, config.skyeye_copy_state_file)


def _has_skyeye_position(address: str, mode: str) -> bool:
    """Same reasoning as every other signal source's own position check --
    only mirror an exit for a position THIS source opened, never a
    screening/other-source position that happens to share the token."""
    positions = (paper_trader.get_portfolio() if mode == "paper" else executor.get_positions())["positions"]
    pos = positions.get(address)
    return pos is not None and pos.get("source") == _SOURCE


async def _handle_buy_signal(trade: dict) -> None:
    address = trade.get("base_address")
    symbol = (trade.get("base_token") or {}).get("symbol")
    if not address:
        return
    if address in _EXCLUDED_BASE_ADDRESSES:
        return

    try:
        # security.check_arbitrary_token_candidate() is async and does its
        # own internal thread-offloading + shared rate_limiter reporting
        # (2026-09-15 audit fix) -- no asyncio.to_thread() wrapper needed
        # here anymore.
        result = await security.check_arbitrary_token_candidate(address, config.chain)
    except gmgn.GmgnCliError:
        log.exception("skyeye_copy: security check failed for %s (%s) -- skipping this signal.", symbol, address)
        return

    if not result.passed:
        log.info("skyeye_copy: %s (%s) signal REJECTED -- %s", symbol, address, "; ".join(result.reasons))
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
    empty_trenches = security.SecurityResult(passed=True, reasons=[])

    tags = ((trade.get("maker_info") or {}).get("tags")) or []
    log.warning("[bold cyan]SKYEYE-COPY SIGNAL[/bold cyan] %s (%s) -- smart money wallet (%s) opened/added, "
                "buying via the Gentleman's own buy path.", symbol, address, ",".join(tags) or "smart_degen")

    import bot.screener as screener
    await screener._autonomous_buy(candidate, empty_trenches, result, source=_SOURCE)


async def _handle_sell_signal(trade: dict) -> None:
    address = trade.get("base_address")
    symbol = (trade.get("base_token") or {}).get("symbol")
    if not address:
        return

    mode = trading_control.get_mode()
    if mode not in ("paper", "live"):
        return
    if not _has_skyeye_position(address, mode):
        return

    log.warning("[bold cyan]SKYEYE-COPY EXIT SIGNAL[/bold cyan] %s (%s) -- a smart money wallet closed/reduced, "
                "mirroring the exit.", symbol, address)
    try:
        if mode == "paper":
            await asyncio.to_thread(paper_trader.simulate_sell, address, "skyeye_copy_exit")
        else:
            await asyncio.to_thread(executor.execute_sell, address, "skyeye_copy_exit")
    except Exception:
        log.exception("skyeye_copy: mirror-sell failed for %s (%s) -- position stays open, normal exit "
                       "management (stop-loss/take-profit/stale) still protects it.", symbol, address)


async def _poll_once() -> None:
    state = _load_state()

    try:
        trades = await asyncio.to_thread(
            gmgn.get_smartmoney_trades, chain=config.chain, limit=config.skyeye_copy_fetch_limit,
        )
        rate_limiter.reset()
    except gmgn.GmgnRateLimitError as exc:
        # Shared backoff (2026-09-15 audit fix) -- see wallet_follow.py's
        # own comment on this same fix across all 5 signal sources built
        # that day for the full reasoning. Especially relevant here: this
        # is the highest-volume signal source of the five (weight-1 calls,
        # but a much bigger aggregate feed), so it's the most likely of
        # the five to actually trip a real rate limit.
        log.warning("skyeye_copy: hit GMGN's rate limit, backing off before the next cycle.")
        await rate_limiter.handle_hit(exc.retry_after_secs)
        return
    except gmgn.GmgnCliError:
        log.exception("skyeye_copy: poll failed -- skipping this cycle.")
        return

    seen: set[str] = set(state.get("seen_tx_hashes", []))

    if not state.get("initialized"):
        # First poll ever -- seed the seen-set from everything already in
        # this batch WITHOUT acting on any of it, same reasoning as every
        # other signal source's own first-run bug avoidance.
        new_seen = list(seen)
        for t in trades:
            h = t.get("transaction_hash")
            if h and h not in seen:
                seen.add(h)
                new_seen.append(h)
        state["seen_tx_hashes"] = new_seen
        state["initialized"] = True
        _save_state(state)
        log.info("skyeye_copy: first poll, baseline seeded with %d trade(s) -- watching for new activity from here.",
                  len(new_seen))
        return

    new_trades = [t for t in trades if t.get("transaction_hash") and t.get("transaction_hash") not in seen]
    if not new_trades:
        return
    # API returns newest-first (same convention as every other gmgn-cli
    # trade feed) -- reverse so signals are handled in the order they
    # actually happened.
    new_trades.reverse()

    new_seen_list = list(seen)
    for trade in new_trades:
        h = trade.get("transaction_hash")
        try:
            side = trade.get("side")
            event = trade.get("is_open_or_close")
            if side == "buy" and event == _OPENED_OR_ADDED:
                await _handle_buy_signal(trade)
            elif side == "sell" and event == _CLOSED_OR_REDUCED:
                await _handle_sell_signal(trade)
            # else: side/event combination that isn't a real buy-open or
            # sell-close signal (e.g. a sell that's itself an add on the
            # short side isn't a real combination GMGN returns here) --
            # nothing to do.
        except Exception:
            log.exception("skyeye_copy: signal handling crashed for %s -- continuing with the rest of this batch.",
                           (trade.get("base_token") or {}).get("symbol"))
        if h and h not in seen:
            seen.add(h)
            new_seen_list.append(h)

    state["seen_tx_hashes"] = new_seen_list
    _save_state(state)


async def run_forever() -> None:
    if not config.skyeye_copy_enabled:
        log.info("skyeye_copy: disabled (SKYEYE_COPY_ENABLED=0), loop not starting.")
        return
    log.warning("skyeye_copy: watching GMGN's smart-money feed every %.0fs.", config.skyeye_copy_poll_interval_secs)
    while True:
        try:
            with file_lock.cross_process_lock(config.skyeye_copy_state_lock_file):
                await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad cycle must never kill the whole loop
            log.exception("skyeye_copy: poll cycle crashed -- continuing on the next one.")
        await asyncio.sleep(config.skyeye_copy_poll_interval_secs)
