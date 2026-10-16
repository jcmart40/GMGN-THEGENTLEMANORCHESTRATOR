"""Paper-mode trading: simulates fills against REAL live prices (via
bot/gmgn_client.get_token_info) into a virtual portfolio file -- no
gmgn-cli swap call, no real funds at risk, no private key ever touched.
Lets the human-approval flow (dashboard/api.py) be exercised end-to-end
before real execution (config.py's "live" mode) is built.

Same atomic temp-file-then-os.replace pattern as bot/screener.py's other
staging files.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time

import bot.file_lock as file_lock
import bot.gmgn_client as gmgn
import bot.trade_history as trade_history
from config import config
# ai_review imported lazily inside the notify call to avoid any import-order
# sensitivity at module load; sets up no cycle (ai_review depends only on
# trade_history/file_lock/config).

log = logging.getLogger("gmgn-agent")

# Same real race found and fixed in bot/executor.py 2026-09-09, applies
# identically here: simulate_buy() runs on a background thread
# (screener.py calls it via asyncio.to_thread), while update_last_price()/
# simulate_sell() run directly on the main event-loop thread (called from
# exit_manager.py's _apply_paper_exit()). Both read-modify-write the same
# paper_portfolio.json file. Even though paper mode risks no real funds,
# a silently lost position here corrupts the exact data the AI's review
# loop learns from -- same threading.Lock fix, same reasoning.
#
# SAME cross-process gap as executor.py too (see bot/file_lock.py's own
# docstring): tui.py imports this exact module directly and gets its own
# separate _portfolio_lock instance, invisible to bot.screener's process.
# The file lock below is what actually closes that; the threading.Lock
# stays as the cheap in-process layer alongside it.
_portfolio_lock = threading.Lock()


class PriceUnavailable(Exception):
    """Raised when a real price can't be fetched -- fail closed rather than
    record a simulated fill against a fabricated/zero price."""


class PositionError(Exception):
    """Raised for a paper-mode state conflict (buy on an already-open
    position, sell/close on one that isn't open)."""


class ImplausiblePrice(Exception):
    """Raised when a fetched exit price moves implausibly far from entry
    -- see MAX_PLAUSIBLE_PRICE_MOVE_FRACTION below for the real incidents
    this responds to. The position is NOT closed and stays open exactly
    as it was; caller should treat this like PriceUnavailable (retry
    later, nothing lost)."""


# Price-quote sanity guard (2026-09-12, same real bugs bot/exit_manager.py's
# own copy of this guard responds to -- see that module's own comment for
# the full ANTH/SCAT incident writeup and the reasoning behind 100x as the
# threshold. exit_manager.py's guard only covers the AUTOMATIC exit-check
# loop (it pre-validates BEFORE ever calling simulate_sell()); this one
# covers simulate_sell() itself directly, closing the real gap found live
# the same night: tui.py's manual sell ("s" / number-key hotkeys) calls
# this function directly with no known_price, fetching its own price with
# NO pre-check at all -- a manual sell at the exact moment GMGN returned a
# corrupted quote would have recorded the same kind of bogus PnL ANTH/SCAT
# did, completely unprotected. Checked unconditionally here (not just when
# known_price is None) -- redundant-but-harmless for the already-validated
# automatic path, and the only way to actually close the manual-sell gap.
MAX_PLAUSIBLE_PRICE_MOVE_FRACTION = 100.0  # 100x, i.e. a 10000% move


def _default_portfolio() -> dict:
    return {"positions": {}, "closed": [], "realized_pnl_sol": 0.0}


def _load() -> dict:
    if not os.path.exists(config.paper_portfolio_file):
        return _default_portfolio()
    try:
        with open(config.paper_portfolio_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("positions", {})
        data.setdefault("closed", [])
        data.setdefault("realized_pnl_sol", 0.0)
        return data
    except Exception:
        log.exception("Paper portfolio file unreadable -- starting fresh rather than blocking paper trading.")
        return _default_portfolio()


def _save(portfolio: dict) -> None:
    directory = os.path.dirname(config.paper_portfolio_file) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(portfolio, f, indent=2)
    os.replace(tmp, config.paper_portfolio_file)


def _get_live_price(address: str) -> float:
    """Real price lookup -- fail closed: missing/None/zero price means we
    cannot honestly simulate a fill, so raise rather than record a fake
    entry at 0."""
    try:
        info = gmgn.get_token_info(address, chain=config.chain)
    except gmgn.GmgnCliError as exc:
        raise PriceUnavailable(f"token info lookup failed for {address}: {exc}") from exc
    price = (info.get("price") or {}).get("price")
    if not price:
        raise PriceUnavailable(f"no live price returned for {address} (token info: {info.get('price')!r})")
    return float(price)


def get_portfolio() -> dict:
    return _load()


def update_last_price(address: str, price: float, market_cap: float | None = None,
                       liquidity: float | None = None) -> tuple[float | None, float | None]:
    """Cache the most recently fetched live price onto an open position, so
    a cheap reader (tui.py) can show current price/unrealized PnL without
    making its own API call on every render tick. Written by
    bot/exit_manager.py on every check cycle (it already fetches this price
    to evaluate stop-loss/take-profit) -- mirrors Axiom-bot's own
    Position.last_price_sol, which main.py's polling loop keeps updated the
    same way. Silent no-op if the position closed between the price fetch
    and this write (a real, harmless race under this project's own
    read-then-write file convention).

    Also tracks peak_pnl_pct (2026-09-06, user's explicit request: "he
    doesn't have to wait for the 50% TP to sell if he sees the trade
    starting to revert and he has profit") -- the highest pnl_pct this
    position has ever reached, so bot/exit_manager.py can detect a
    reversal from that peak and exit early on a real profit giveback,
    not just the fixed take-profit threshold. Returns the (possibly
    just-updated) peak so the caller doesn't need a second read to get
    it -- same one-write, no-extra-read pattern the rest of this file
    already uses.

    market_cap (2026-09-12, user's explicit request: "the only thing
    missing on the dashboard is market cap on the tokens he is trading" /
    "so I can see where the market cap was when he buys a token"):
    bot/exit_manager.py computes this from the SAME get_token_info() call
    it already makes for price every cycle (price x circulating_supply,
    both already in that response) -- no extra API call. Cached as
    last_market_cap alongside last_price; entry_market_cap (set once at
    buy time in simulate_buy() below) stays fixed so tui.py can show
    entry -> live side by side.

    Also tracks peak_liquidity (2026-09-19, see config.py's
    liquidity_drain_mcap_threshold_usd/liquidity_drain_pct docstring) --
    same highest-value-ever-seen pattern as peak_pnl_pct, so
    bot/exit_manager.py can detect a real drain from this position's OWN
    peak rather than an arbitrary fixed floor. Returns (peak_pnl_pct,
    peak_liquidity) now instead of a bare float -- both callers
    (bot/exit_manager.py) updated to unpack the tuple."""
    with file_lock.cross_process_lock(config.paper_portfolio_lock_file):
        with _portfolio_lock:
            portfolio = _load()
            pos = portfolio["positions"].get(address)
            if pos is None:
                return None, None
            pos["last_price"] = price
            if market_cap is not None:
                pos["last_market_cap"] = market_cap
            entry_price = pos.get("entry_price", 0.0)
            pnl_pct = ((price - entry_price) / entry_price * 100.0) if entry_price else 0.0
            peak = max(pos.get("peak_pnl_pct", 0.0), pnl_pct)
            pos["peak_pnl_pct"] = peak
            peak_liquidity = pos.get("peak_liquidity")
            if liquidity is not None:
                pos["last_liquidity"] = liquidity
                peak_liquidity = max(peak_liquidity, liquidity) if peak_liquidity is not None else liquidity
                pos["peak_liquidity"] = peak_liquidity
            _save(portfolio)
            return peak, peak_liquidity


def simulate_buy(candidate: dict) -> dict:
    """Open a simulated position sized at config.trade_size_sol SOL, entry
    price pulled live at the moment of approval -- same "real market data,
    fake money" principle as Axiom-bot's own paper trader."""
    address = candidate.get("address")
    if not address:
        raise PositionError("candidate has no address")

    with file_lock.cross_process_lock(config.paper_portfolio_lock_file):
        with _portfolio_lock:
            portfolio = _load()
            if address in portfolio["positions"]:
                raise PositionError(f"paper position already open for {address}")

    entry_price = _get_live_price(address)
    entry_ts = time.time()
    # Signal-to-buy latency (2026-09-16, user + son's explicit request,
    # Axiom-bot's own trigger_ts pattern) -- trigger_ts is stamped by
    # whichever signal source first detected this candidate (see
    # bot/screener.py's screen_once()/bot/dev_snipe.py/bot/token_snipe.py).
    # None for a source that doesn't set it yet -- fail soft to an absent
    # latency_ms rather than a crash, same fail-soft reasoning as every
    # other optional-field read in this project.
    trigger_ts = candidate.get("trigger_ts")
    latency_ms = (entry_ts - trigger_ts) * 1000 if trigger_ts is not None else None
    position = {
        "address": address,
        "symbol": candidate.get("symbol"),
        "entry_price": entry_price,
        "amount_sol": config.trade_size_sol,
        "entry_ts": entry_ts,
        "trigger_ts": trigger_ts,
        "latency_ms": latency_ms,
        "gmgn_link": candidate.get("gmgn_link"),
        # 2026-09-12, user's explicit request -- see update_last_price()'s
        # own docstring above for the full reasoning. From the same
        # candidate dict bot/screener.py already built from trenches data,
        # no extra API call.
        "entry_market_cap": candidate.get("market_cap"),
        # 2026-09-15 -- which signal source found this candidate (e.g.
        # "screening" vs "wallet_follow"), for dashboard/reporting purposes
        # only. Deliberately does NOT change any trading/exit logic --
        # one unified engine handles every position the same way regardless
        # of source (user's explicit architecture call, see
        # bot/wallet_follow.py's own module docstring). Defaults to
        # "screening" so the existing screener.py call site needs no change.
        "source": candidate.get("source", "screening"),
    }
    # Re-read fresh under the lock rather than reusing the pre-network-call
    # snapshot above -- same reasoning as executor.py's execute_buy() fix.
    #
    # Re-checks for a duplicate here too (2026-09-15 audit fix, real gap
    # found -- not present before today): without this, two concurrent
    # simulate_buy() calls for the SAME address (now realistic with 5
    # signal sources able to independently flag the same trending token at
    # once) could both pass the early check above before either writes,
    # and the second write here would silently overwrite the first
    # position record -- no real money lost in paper mode, but a real,
    # silent loss of one signal source's own trade record, corrupting
    # dashboard/AI-review data with no error or log trail.
    with file_lock.cross_process_lock(config.paper_portfolio_lock_file):
        with _portfolio_lock:
            portfolio = _load()
            if address in portfolio["positions"]:
                raise PositionError(f"paper position already open for {address} (caught by the pre-write re-check)")
            portfolio["positions"][address] = position
            _save(portfolio)
    log.info("PAPER BUY: %s (%s) @ %.10f, size=%.3f SOL", position["symbol"], address, entry_price, config.trade_size_sol)
    return position


def simulate_sell(address: str, reason: str = "manual", known_price: float | None = None) -> dict:
    """Close a simulated position at the current live price, recording
    realized (paper) PnL.

    known_price (2026-09-11, real fix found live): bot/exit_manager.py's
    own _check_positions_batched() already fetches a fresh live price for
    every position EVERY check cycle to decide whether to exit at all --
    _apply_paper_exit() was then calling this function, which fetched its
    OWN second, completely independent price a beat later just to record
    exit_price. On a thin, brand-new pool that can crash 90%+ within a
    couple of seconds, that redundant ~680ms extra round-trip (measured
    live) was pure extra exposure between "decided to sell" and "actually
    recorded the sale" -- it could only ever make a fast-crashing exit
    look worse, never better. Callers that already have a fresh price in
    hand (exit_manager.py) should pass it here directly; tui.py's manual
    sell has no fresh price on hand and still fetches its own, exactly as
    before -- this parameter is additive, not a behavior change for that
    path."""
    with file_lock.cross_process_lock(config.paper_portfolio_lock_file):
        with _portfolio_lock:
            portfolio = _load()
            position = portfolio["positions"].pop(address, None)
            if position is not None:
                _save(portfolio)
    if position is None:
        raise PositionError(f"no open paper position for {address}")

    exit_price = known_price if known_price is not None else _get_live_price(address)
    entry_price = position["entry_price"]
    raw_pnl_pct = (exit_price - entry_price) / entry_price if entry_price else 0.0
    if abs(raw_pnl_pct) > MAX_PLAUSIBLE_PRICE_MOVE_FRACTION:
        # Restore the position exactly as it was popped above -- this sell
        # never happened, nothing about the open position should change.
        with file_lock.cross_process_lock(config.paper_portfolio_lock_file):
            with _portfolio_lock:
                portfolio = _load()
                portfolio["positions"][address] = position
                _save(portfolio)
        log.warning("Implausible exit price for %s (%s): entry=%.10g exit=%.10g (%.0f%% move) -- "
                     "distrusting this quote, position restored, not closed.",
                     position.get("symbol"), address, entry_price, exit_price, raw_pnl_pct * 100)
        raise ImplausiblePrice(f"implausible price move for {address}: {raw_pnl_pct*100:.0f}%")
    raw_pnl_sol = position["amount_sol"] * raw_pnl_pct
    # Paper mode's price lookup is a plain quote, not an executed swap, so
    # (unlike a live close, whose exit_price already has GMGN's real fee
    # baked in) raw_pnl above has never reflected any fee cost. Deduct the
    # measured round-trip fee here so displayed PnL is a realistic preview
    # of live, not an optimistic one -- see config.paper_simulated_fee_pct's
    # own comment for where 0.115 comes from. raw_pnl_pct/raw_pnl_sol are
    # kept on the record too so nothing is silently discarded.
    pnl_pct = raw_pnl_pct - config.paper_simulated_fee_pct
    pnl_sol = position["amount_sol"] * pnl_pct

    closed = {
        **position,
        "exit_price": exit_price,
        "exit_ts": time.time(),
        "pnl_pct": pnl_pct,
        "pnl_sol": pnl_sol,
        "raw_pnl_pct": raw_pnl_pct,
        "raw_pnl_sol": raw_pnl_sol,
        "simulated_fee_pct": config.paper_simulated_fee_pct,
        "reason": reason,
    }
    # Re-read fresh under the lock rather than reusing the pre-network-call
    # snapshot -- same reasoning as executor.py's execute_sell() fix.
    with file_lock.cross_process_lock(config.paper_portfolio_lock_file):
        with _portfolio_lock:
            portfolio = _load()
            portfolio["closed"].append(closed)
            portfolio["closed"] = portfolio["closed"][-1000:]  # bounded, same reasoning as other staging files
            portfolio["realized_pnl_sol"] = portfolio.get("realized_pnl_sol", 0.0) + pnl_sol
            _save(portfolio)
    log.info("PAPER SELL: %s (%s) @ %.10f, pnl=%.5f SOL (%.2f%%), reason=%s",
              closed["symbol"], address, exit_price, pnl_sol, pnl_pct * 100, reason)
    trade_history.record_trade({**closed, "mode": "paper"})
    from bot import ai_review as _ai_review
    _ai_review.notify_trade_outcome({**closed, "mode": "paper"})
    return closed


def get_summary() -> dict:
    portfolio = _load()
    closed = portfolio["closed"]
    wins = sum(1 for c in closed if c["pnl_sol"] > 0)
    return {
        "open_positions": len(portfolio["positions"]),
        "closed_trades": len(closed),
        "win_rate": (wins / len(closed)) if closed else None,
        "realized_pnl_sol": portfolio.get("realized_pnl_sol", 0.0),
    }
