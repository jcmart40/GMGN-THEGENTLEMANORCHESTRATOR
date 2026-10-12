"""Real trade execution: approve -> actual on-chain swap via gmgn-cli.

Two independent safety gates, deliberately never bypassed here:
  1. GMGN's own CLI-level gate: `swap --yes` requires the env var
     GMGN_ALLOW_AUTOMATED_TRADES=1 (confirmed via `gmgn-cli swap --help`,
     2026-09-05) -- a deliberate friction point built into the CLI itself.
     This module never sets that variable; it only checks whether the user
     has set it themselves, same as it never touches GMGN_PRIVATE_KEY.
  2. This project's own gate: bot/trading_control.py's mode must be "live"
     -- checked again here (defense in depth) even though the only caller
     today (dashboard/api.py) already checks it before calling in.

Fail closed: any missing precondition raises ExecutionBlocked with a clear
reason, never a silent no-op and never a silent fallback to paper/off
behavior. Positions are tracked in their own file (config.live_positions_file),
same atomic temp-file-then-os.replace pattern as every other staging file in
this project.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from decimal import Decimal

import bot.ai_review as ai_review
import bot.file_lock as file_lock
import bot.gmgn_client as gmgn
import bot.trade_history as trade_history
import bot.trading_control as trading_control
from config import config

log = logging.getLogger("gmgn-agent")

# Real race found and fixed 2026-09-09: execute_buy() runs on a background
# thread (screener.py calls it via asyncio.to_thread), while
# update_last_price()/execute_sell() run directly on the main event-loop
# thread (exit_manager.py's own comment claimed this was fine since these
# are "fast local file ops" -- true in isolation, but that reasoning never
# accounted for a SEPARATE thread also read-modify-writing the same file
# concurrently). update_last_price() alone fires every exit_check_interval_secs
# (2s) for every open position, so the race window was nearly constant, not
# a rare edge case -- confirmed live: a real buy (MIDAS) landed on-chain but
# its write to this file was silently lost, leaving a real, unprotected
# open position invisible to exit management entirely. A plain
# threading.Lock (not asyncio.Lock, which only protects against other
# coroutines on the SAME event loop, not genuine cross-thread races) around
# every read-modify-write cycle on this file closes the gap.
#
# SECOND gap found the same night, same audit: this threading.Lock only
# protects against races WITHIN this one process. tui.py runs as a
# completely separate `python tui.py` OS process that imports this exact
# module and calls these exact functions directly (its own "s"/digit-key
# sell hotkeys) -- it gets its OWN separate _positions_lock instance, which
# knows nothing about this process's lock. See bot/file_lock.py's own
# docstring for the real, OS-level cross-process lock that closes this --
# used alongside (not instead of) this threading.Lock below: the cheap
# in-process lock avoids unnecessary OS lock contention between threads in
# THIS process; the file lock is what actually stops tui.py's process and
# this one from racing on the same file.
_positions_lock = threading.Lock()

LAMPORTS_PER_SOL = 1_000_000_000
# Solana's native mint address for wrapped SOL -- gmgn-cli swap treats this
# as the "SOL" input/output token for a buy/sell against any other mint.
SOL_MINT = "So11111111111111111111111111111111111111112"

# Real bug found live 2026-09-13: execute_sell()'s wallet-holdings check
# ("held is None") can mean two very different things -- a very recent buy
# that hasn't confirmed on-chain yet (transient, restoring and retrying is
# correct), or a position that was already closed OUTSIDE this bot entirely
# (a manual sell on the GMGN platform itself, invisible to this code) --
# permanent, restoring is WRONG. Confirmed live: a position (gigalon) whose
# real tokens were already gone from the wallet kept getting silently
# restored forever, producing a fully fictional "open" position with a
# real-looking, real-time-updating price and a fake unrealized gain,
# cycling in an endless pop-then-restore loop. 90s is generous -- real
# Solana confirmation is sub-second to a few seconds -- so this errs
# toward NOT declaring a position gone prematurely; it only kicks in once
# a position has clearly had time to confirm and still shows no balance.
_HOLDINGS_NOT_FOUND_GRACE_SECS = 90.0

# Real bug found live 2026-09-14: execute_buy()'s post-swap file write (the
# block that records a new position into live_positions_file) had no retry
# protection at all. Confirmed live: 9 real buys (FLAME, NASDAWG, and 7
# others) had their swaps succeed on-chain -- real SOL spent, real tokens
# received, verified directly against actual wallet holdings -- but were
# never written to this file, leaving them completely unmonitored: no
# stop-loss, no take-profit, invisible to every part of this system. By
# the time this write runs, real money is already spent and tokens already
# received -- a single transient failure here (lock contention, a momentary
# disk hiccup) must not be the end of the story the way it silently was
# before.
_BUY_PERSIST_RETRY_ATTEMPTS = 3
_BUY_PERSIST_RETRY_DELAY_SECS = 0.5


class ExecutionBlocked(Exception):
    """Raised when a live-execution precondition isn't met. Callers must
    never interpret this as "nothing happened, safe to ignore" -- it means
    real execution was deliberately refused."""


def _check_preconditions() -> None:
    if trading_control.get_mode() != "live":
        raise ExecutionBlocked("trading mode is not 'live'")
    if os.environ.get("GMGN_ALLOW_AUTOMATED_TRADES") != "1":
        raise ExecutionBlocked(
            "GMGN_ALLOW_AUTOMATED_TRADES=1 is not set in this process's environment -- "
            "gmgn-cli swap's own --yes flag requires it. This is GMGN's own deliberate "
            "gate, not something this app sets for you -- set it yourself, outside this "
            "app, to actually enable automated trades."
        )
    if not config.wallet_address:
        raise ExecutionBlocked("config.wallet_address (GMGN_WALLET_ADDRESS) is not configured")


def _default_state() -> dict:
    return {"positions": {}, "closed": []}


def _load_positions() -> dict:
    if not os.path.exists(config.live_positions_file):
        return _default_state()
    try:
        with open(config.live_positions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("positions", {})
        data.setdefault("closed", [])
        return data
    except Exception:
        log.exception("Live positions file unreadable -- starting fresh rather than "
                       "blocking execution (real on-chain state is the source of truth, "
                       "not this cache).")
        return _default_state()


def _save_positions(data: dict) -> None:
    directory = os.path.dirname(config.live_positions_file) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, config.live_positions_file)


def get_positions() -> dict:
    return _load_positions()


def update_last_price(address: str, price: float, market_cap: float | None = None,
                       liquidity: float | None = None) -> tuple[float | None, float | None]:
    """Same purpose as paper_trader.update_last_price() -- cache the most
    recently fetched live price onto an open position so tui.py can show
    current price/unrealized PnL cheaply, AND track peak_pnl_pct (same
    reasoning as paper_trader's own version -- see that function's
    docstring) so exit management can detect a real-money position
    reverting from its peak while still in profit, not just wait for the
    fixed take-profit threshold. Written by bot/exit_manager.py on every
    check cycle. Returns (peak_pnl_pct, peak_liquidity) now, mirroring
    paper_trader.update_last_price()'s own 2026-09-19 change -- see that
    function's docstring for the liquidity-drain reasoning. (None, None)
    if the position isn't open.

    market_cap (2026-09-12, same request/reasoning as
    paper_trader.update_last_price()'s own docstring) -- cached as
    last_market_cap alongside last_price."""
    with file_lock.cross_process_lock(config.live_positions_lock_file):
        with _positions_lock:
            positions = _load_positions()
            pos = positions["positions"].get(address)
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
            _save_positions(positions)
            return peak, peak_liquidity


def execute_buy(candidate: dict) -> dict:
    """Real buy: SOL -> candidate's token, via gmgn-cli swap. Raises
    ExecutionBlocked if any precondition fails -- dashboard/api.py is
    expected to catch this and record it on the candidate's own
    "execution" field, same shape as it already does for paper_trader's
    exceptions."""
    _check_preconditions()
    address = candidate.get("address")
    if not address:
        raise ExecutionBlocked("candidate has no address")

    with file_lock.cross_process_lock(config.live_positions_lock_file):
        with _positions_lock:
            positions = _load_positions()
            if address in positions["positions"]:
                raise ExecutionBlocked(f"live position already open for {address}")

    # Entry price recorded the same way paper_trader.py does (a live
    # get_token_info() read at fill time) rather than trying to parse a
    # price out of submit_swap()'s own response -- that response shape has
    # never actually been captured from a real successful swap (this
    # project has no confirmed swap-enabled wallet yet), so trusting an
    # unverified field there would repeat exactly the kind of
    # unverified-assumption bug this project has been bitten by before.
    # Fail-closed: no entry price means bot/exit_manager.py can never
    # compute this position's PnL, so refuse the position outright rather
    # than open one exit management can't ever act on.
    try:
        entry_price = gmgn.get_token_info(address, chain=config.chain).get("price", {}).get("price")
    except gmgn.GmgnCliError as exc:
        raise ExecutionBlocked(f"could not fetch entry price for {address}: {exc}") from exc
    if not entry_price:
        raise ExecutionBlocked(f"no live price available for {address} -- refusing to open an unmonitorable position")

    # Re-check for a duplicate position IMMEDIATELY before the real swap
    # (2026-09-15 audit fix, real gap found -- not present before today):
    # the early check above happens before the slow get_token_info() call
    # (a real network round-trip), leaving a window where a second
    # concurrent execute_buy() call for the SAME address (now realistic
    # with 5 signal sources able to independently flag the same trending
    # token at once, on top of screening) could pass the same early check
    # before either one writes -- resulting in two real on-chain swaps for
    # one intended position. This second check happens right at the last
    # possible moment before committing real money, inside the lock but
    # released again before the slow submit_swap() call below (holding a
    # lock across that entire network round-trip would block every OTHER
    # concurrent buy attempt for its full duration, a worse trade-off than
    # the now much narrower window this leaves between the check and the
    # swap call itself).
    with file_lock.cross_process_lock(config.live_positions_lock_file):
        with _positions_lock:
            positions = _load_positions()
            if address in positions["positions"]:
                raise ExecutionBlocked(f"live position already open for {address} (caught by the pre-swap re-check)")

    amount_lamports = int(config.trade_size_sol * LAMPORTS_PER_SOL)
    # Native server-side TP/SL, attached to this same buy (2026-09-26) -- see
    # bot/gmgn_client.py's submit_swap() docstring for the full reasoning.
    # Built from whatever the Gentleman's effective stop_loss_pct/
    # take_profit_pct are AT THIS EXACT MOMENT -- frozen into the order at
    # entry, same as any real limit order; if his AI free-will tuning moves
    # those dials later, this specific position's native order does NOT
    # follow, only exit_manager.py's own polling checks (which always read
    # the live current value) do. Non-fatal by design: a problem building
    # this must never block the buy itself -- the polling exit_manager is
    # the complete fallback either way, this is a faster backstop for
    # exactly the stop-loss/take-profit overshoot problem real emergency-
    # drawdown data showed (every single one blew 10-80+ points past the
    # line under the 2s poll gap alone).
    condition_orders = None
    try:
        stop_loss_pct = ai_review.get_effective_stop_loss_pct()
        take_profit_pct = ai_review.get_effective_take_profit_pct()
        orders = []
        if take_profit_pct > 0:
            orders.append({
                "order_type": "profit_stop", "side": "sell",
                "price_scale": str(take_profit_pct), "sell_ratio": "100",
            })
        if stop_loss_pct > 0:
            orders.append({
                "order_type": "loss_stop", "side": "sell",
                "price_scale": str(stop_loss_pct), "sell_ratio": "100",
            })
        if orders:
            condition_orders = orders
    except Exception:
        log.exception("Failed to build native condition-orders for %s -- "
                       "proceeding with a plain buy, polling exit_manager "
                       "remains the sole protection for this position.", address)
        condition_orders = None

    result = gmgn.submit_swap(
        chain=config.chain, from_wallet=config.wallet_address,
        input_token=SOL_MINT, output_token=address,
        amount=amount_lamports, slippage=config.slippage_pct,
        priority_fee=config.priority_fee_sol,
        condition_orders=condition_orders,
        sell_ratio_type="buy_amount" if condition_orders else None,
    )
    entry_ts = time.time()
    # Signal-to-buy latency (2026-09-16) -- see paper_trader.py's own
    # simulate_buy() copy of this for the full reasoning; identical pattern
    # for the live path.
    trigger_ts = candidate.get("trigger_ts")
    latency_ms = (entry_ts - trigger_ts) * 1000 if trigger_ts is not None else None
    position = {
        "address": address,
        "symbol": candidate.get("symbol"),
        "amount_sol": config.trade_size_sol,
        "entry_price": float(entry_price),
        "entry_ts": entry_ts,
        "trigger_ts": trigger_ts,
        "latency_ms": latency_ms,
        "buy_result": result,
        # 2026-09-12, same request as paper_trader.simulate_buy()'s own copy
        # of this field -- from the same candidate dict, no extra API call.
        "entry_market_cap": candidate.get("market_cap"),
        # 2026-09-15 -- see paper_trader.simulate_buy()'s own copy of this
        # field for the full reasoning. Reporting-only, never affects
        # trading/exit logic.
        "source": candidate.get("source", "screening"),
        # 2026-09-26 -- real record of what native TP/SL (if any) got
        # attached at buy time, and at what values, since those are frozen
        # at entry and won't move if the AI retunes stop_loss_pct/
        # take_profit_pct afterward. None means the build failed or both
        # were disabled (<=0) -- exit_manager.py's polling loop is this
        # position's only protection in that case, same as before this
        # feature existed.
        "native_condition_orders": condition_orders,
    }
    # Re-read fresh under the lock rather than reusing the `positions` dict
    # read above -- that read happened before the slow network calls
    # (get_token_info/submit_swap, up to ~30s combined), and another writer
    # (update_last_price()/execute_sell(), both running on the main thread
    # while this runs on to_thread's background thread) could easily have
    # changed the file in that window. Writing back a stale copy is exactly
    # the bug that silently dropped a real position (found 2026-09-09).
    #
    # Retry-protected (2026-09-14, see _BUY_PERSIST_RETRY_ATTEMPTS's own
    # docstring above) -- retries a few times with a short pause; if it
    # still can't write after that, this raises LOUDLY instead of quietly
    # returning, so the caller and the log both know a real, real-money
    # position exists that this file has no record of, rather than
    # pretending everything's fine.
    last_exc: Exception | None = None
    for attempt in range(1, _BUY_PERSIST_RETRY_ATTEMPTS + 1):
        try:
            with file_lock.cross_process_lock(config.live_positions_lock_file):
                with _positions_lock:
                    positions = _load_positions()
                    positions["positions"][address] = position
                    _save_positions(positions)
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 -- real money already spent, any failure here must be loud, never silent
            last_exc = exc
            log.exception("Failed to save live position %s (%s) after a REAL swap already executed "
                           "(attempt %d/%d)%s.", position["symbol"], address, attempt,
                           _BUY_PERSIST_RETRY_ATTEMPTS,
                           " -- retrying" if attempt < _BUY_PERSIST_RETRY_ATTEMPTS else "")
            if attempt < _BUY_PERSIST_RETRY_ATTEMPTS:
                time.sleep(_BUY_PERSIST_RETRY_DELAY_SECS)
    if last_exc is not None:
        log.critical("REAL MONEY UNTRACKED: %s (%s) swap succeeded (buy_result=%r) but could not be "
                      "saved to %s after %d attempts -- this position is completely unmonitored "
                      "(no stop-loss/take-profit protection at all) until found and added back "
                      "manually. Last error: %s", position["symbol"], address, result,
                      config.live_positions_file, _BUY_PERSIST_RETRY_ATTEMPTS, last_exc)
        raise ExecutionBlocked(
            f"REAL swap for {address} succeeded but the position could not be saved after "
            f"{_BUY_PERSIST_RETRY_ATTEMPTS} attempts -- tokens are in the wallet but UNTRACKED, "
            f"check logs and add back manually: {last_exc}"
        ) from last_exc
    log.warning("LIVE BUY: %s (%s) size=%.3f SOL -- result=%r",
                position["symbol"], address, config.trade_size_sol, result)
    return position


def execute_sell(address: str, reason: str = "manual") -> dict:
    """Real sell: candidate's token -> SOL, for whatever the wallet
    ACTUALLY holds right now (not the original SOL-sized estimate from
    the buy -- token amount received is a function of live price/slippage
    at fill time, never known in advance).

    Reads the real balance from `portfolio holdings` -- verified live
    2026-09-05 that `balance` there is a decimal STRING (human-readable
    amount), NOT raw base units, and the token's own address lives under
    the nested `token.token_address` field, not a top-level `address` key.
    Converting to the raw integer `swap --amount` needs is done here via
    Decimal (never float, to avoid a precision-loss bug at the exact
    boundary that decides how much gets sold)."""
    _check_preconditions()
    # Lock scope kept tight around each individual read-modify-write below
    # (not held across the slow network calls in between -- get_wallet_holdings/
    # submit_swap can take real wall-clock time) -- same reasoning as
    # execute_buy(). Each restore path re-reads fresh right before writing
    # rather than reusing an earlier snapshot, so a concurrent buy/price
    # update from the other thread in the meantime never gets silently
    # clobbered (found 2026-09-09: this exact pattern lost a real position).
    with file_lock.cross_process_lock(config.live_positions_lock_file):
        with _positions_lock:
            positions = _load_positions()
            position = positions["positions"].pop(address, None)
            if position is not None:
                _save_positions(positions)
    if position is None:
        raise ExecutionBlocked(f"no open live position for {address}")

    def _restore() -> None:
        with file_lock.cross_process_lock(config.live_positions_lock_file):
            with _positions_lock:
                fresh = _load_positions()
                fresh["positions"][address] = position
                _save_positions(fresh)

    try:
        holdings = gmgn.get_wallet_holdings(config.wallet_address, chain=config.chain, limit=100)
    except gmgn.GmgnCliError as exc:
        # The check itself failed (API timeout/error) -- we genuinely don't
        # know whether this position is still real. Restoring is the SAFE
        # default here: losing track of a real, still-open, real-money
        # position (no stop-loss/take-profit protection at all until it's
        # rediscovered) is a far worse outcome than one delayed retry. Only
        # a holdings check that actually SUCCEEDS and confirms no balance,
        # past the grace period below, is ever treated as "genuinely gone."
        _restore()
        raise ExecutionBlocked(
            f"could not check wallet holdings for {address} ({exc}) -- restored, will retry"
        ) from exc

    held = next((h for h in holdings if h.get("token", {}).get("token_address") == address), None)
    if held is None:
        age_secs = time.time() - position.get("entry_ts", time.time())
        if age_secs < _HOLDINGS_NOT_FOUND_GRACE_SECS:
            _restore()  # still plausibly just on-chain confirmation lag right after the buy
            raise ExecutionBlocked(
                f"wallet holdings show no balance for {address} yet ({age_secs:.0f}s since buy) -- "
                f"restored, buy may still be confirming"
            )
        # Past the grace period with a CONFIRMED empty holdings response --
        # see this file's own _HOLDINGS_NOT_FOUND_GRACE_SECS docstring.
        # Close it out for good instead of restoring it again: pnl is
        # genuinely unknown here (we have no idea what it actually sold
        # for, or if it was even a real sale vs. a rug/burn), so record it
        # honestly rather than fabricate a number.
        closed = {
            **position, "exit_price": None, "exit_ts": time.time(),
            "pnl_pct": None, "pnl_sol": None,
            "reason": "closed_externally_or_missing", "sell_result": None,
        }
        with file_lock.cross_process_lock(config.live_positions_lock_file):
            with _positions_lock:
                positions = _load_positions()
                positions["closed"].append(closed)
                positions["closed"] = positions["closed"][-1000:]
                _save_positions(positions)
        log.warning(
            "LIVE POSITION GONE: %s (%s) held no balance after %.0fs -- assuming closed outside "
            "this bot (manual sell, or lost), removing from tracking permanently instead of "
            "restoring it again.", closed["symbol"], address, age_secs,
        )
        trade_history.record_trade({**closed, "mode": "live"})
        raise ExecutionBlocked(
            f"{address} no longer held after {age_secs:.0f}s -- treated as closed externally, "
            f"removed from tracking for good (see logs)"
        )

    decimals = held.get("token", {}).get("decimals")
    balance_str = held.get("balance")
    if decimals is None or not balance_str:
        _restore()
        raise ExecutionBlocked(f"holdings entry for {address} missing decimals/balance -- refusing to guess")

    amount_raw = int(Decimal(balance_str) * (Decimal(10) ** int(decimals)))
    if amount_raw <= 0:
        _restore()
        raise ExecutionBlocked(f"holdings balance for {address} is zero/negative -- nothing to sell")

    result = gmgn.submit_swap(
        chain=config.chain, from_wallet=config.wallet_address,
        input_token=address, output_token=SOL_MINT,
        amount=amount_raw, slippage=config.slippage_pct,
        priority_fee=config.priority_fee_sol,
    )

    # PnL was never computed for a live close before 2026-09-06 (this
    # function only ever recorded sell_result, nothing else) -- fixed here
    # the same fail-soft way: a real exit price the moment the swap is
    # submitted, same get_token_info() call execute_buy()/paper_trader.py
    # already use for entry price. Soft-fail (None, not ExecutionBlocked)
    # since the swap itself already happened by this point -- a missing
    # price here must not make record-keeping block a trade that already
    # executed on-chain.
    exit_price = None
    try:
        exit_price = gmgn.get_token_info(address, chain=config.chain).get("price", {}).get("price")
    except gmgn.GmgnCliError:
        log.exception("Could not fetch exit price for %s after a real sell -- "
                       "pnl will be recorded as unknown, the sell itself still succeeded.", address)
    entry_price = position.get("entry_price")
    pnl_pct = None
    pnl_sol = None
    if exit_price and entry_price:
        pnl_pct = (float(exit_price) - entry_price) / entry_price
        pnl_sol = position["amount_sol"] * pnl_pct

    closed = {
        **position, "exit_price": float(exit_price) if exit_price else None,
        "exit_ts": time.time(), "pnl_pct": pnl_pct, "pnl_sol": pnl_sol,
        "reason": reason, "sell_result": result,
    }
    with file_lock.cross_process_lock(config.live_positions_lock_file):
        with _positions_lock:
            positions = _load_positions()  # re-read fresh, not the pre-network-call snapshot
            positions["closed"].append(closed)
            positions["closed"] = positions["closed"][-1000:]  # bounded, same reasoning as every other staging file
            _save_positions(positions)
    log.warning("LIVE SELL: %s (%s) reason=%s pnl=%s -- result=%r",
                closed["symbol"], address, reason,
                f"{pnl_sol:.5f} SOL ({pnl_pct * 100:.2f}%)" if pnl_sol is not None else "unknown", result)
    trade_history.record_trade({**closed, "mode": "live"})
    return closed
