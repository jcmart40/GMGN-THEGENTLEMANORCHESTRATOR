"""Autonomous exit management: stop-loss / take-profit for every open
position, paper AND live. The one safety net kept in place per the user's
explicit 2026-09-06 direction ("no guardrails" on entries -- no daily loss
cap, no position cap, no cooldown -- but exit protection stays, so a single
bad trade can't run unbounded). Runs continuously alongside bot/screener.py
(see that module's run_forever(), which gathers this loop with its own),
same relationship Axiom-bot's own main.py has between its launch-detection
and check_exits() loops living in one process.

Checks each open position's CURRENT live price against its stored entry
price (both bot/paper_trader.py and bot/executor.py record entry_price the
same way -- a live get_token_info() read at fill time) and closes the
position the instant either threshold is crossed. Fail-open on a single
price-fetch failure for one position (skip it this cycle, try again next
cycle) -- a transient API hiccup must never crash the whole exit-check pass
and leave EVERY position unmonitored.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import bot.ai_review as ai_review
import bot.executor as executor
import bot.gmgn_client as gmgn
import bot.paper_trader as paper_trader
import bot.rate_limiter as rate_limiter
from config import config

log = logging.getLogger("gmgn-agent")

# Stop-loss whipsaw guard (2026-09-17) -- in-process only, deliberately not
# persisted to disk: address -> the time.time() this position was FIRST
# observed breaching stop-loss, continuously since then. Reset (popped) the
# moment a check finds it no longer breaching, or the position closes for
# any reason -- see _decide_exit_reason()/_apply_paper_exit()/
# _apply_live_exit() below. A restart losing this state is harmless (worst
# case: one position's whipsaw guard starts its confirmation window over),
# same fail-soft reasoning as every other soft/derived state in this
# project (seen_mints, rate_limiter's consecutive counter, etc).
_stop_loss_breach_started: dict[str, float] = {}


def _has_queue_pressure() -> bool:
    """True if a "skipped_max_positions" entry exists in
    cleared_candidates.json within the effective queue-pressure lookback --
    i.e. a real, CURRENT candidate is being missed for lack of an open
    slot, not a stale skip from a while ago. Reads the same file
    bot/screener.py's _autonomous_buy() already writes to; no shared
    state/import needed since this is just a read."""
    if not os.path.exists(config.cleared_candidates_file):
        return False
    try:
        with open(config.cleared_candidates_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    cutoff = time.time() - ai_review.get_effective_value("queue_pressure_lookback_secs")
    return any(e.get("status") == "skipped_max_positions" and e.get("ts", 0) >= cutoff for e in data)


def _pnl_pct(entry_price: float, current_price: float) -> float:
    if not entry_price:
        return 0.0
    return (current_price - entry_price) / entry_price * 100.0


# Price-quote sanity guard (2026-09-12, real bugs found live TWO nights in
# a row: ANTH recorded a +98,377% "gain" 9/11 (entry 0.0000977 -> exit
# 9.61), then SCAT recorded +614,927% 9/12 (entry 0.0000567 -> exit
# 0.348) -- both single-handedly blew up the dashboard's Live PnL total
# by thousands of SOL. Both share the same signature: a price that jumps
# straight to a "normal-looking" absolute value (near 1.0 or higher)
# instead of staying in the tiny scientific-notation range every other
# pre-migration pump.fun-style token in this dataset lives in -- almost
# certainly a unit/scale mismatch when a token migrates off the bonding
# curve to a full DEX mid-hold, not a real, capturable price move.
#
# 10000.0 (100x) chosen deliberately ABOVE the biggest CONFIRMED-plausible
# win in this same dataset (three different, verified-distinct-address
# tokens independently landed around +3750-3760%, ~38x, entirely
# consistent with pump.fun's shared bonding-curve math -- not a bug) and
# far BELOW both real incidents (983x and 6149x) -- a wide margin on both
# sides, not a tight guess. A quote that fails this check is treated
# exactly like a failed price fetch (see PriceUnavailable/GmgnCliError
# handling elsewhere in this module): distrusted and skipped this cycle,
# nothing recorded, position stays open untouched, retried fresh next
# cycle -- never silently "fixed" or clamped, since a wrong guess at the
# real price would be its own new source of bad data.
_MAX_PLAUSIBLE_PRICE_MOVE_PCT = 10000.0


def _is_price_plausible(entry_price: float, current_price: float, symbol: str | None) -> bool:
    if not entry_price:
        return True
    move_pct = abs((current_price - entry_price) / entry_price * 100.0)
    if move_pct > _MAX_PLAUSIBLE_PRICE_MOVE_PCT:
        log.warning("Exit check: implausible price quote for %s (entry=%.10g current=%.10g, "
                     "%.0f%% move) -- distrusting this quote, skipping this cycle, will "
                     "re-fetch fresh next cycle.", symbol or "?", entry_price, current_price, move_pct)
        return False
    return True


def _decide_exit_reason(address: str, pnl_pct: float, peak_pnl_pct: float | None, has_queue_pressure: bool,
                         age_secs: float, liquidity: float | None = None, market_cap: float | None = None,
                         peak_liquidity: float | None = None) -> str | None:
    """Shared decision logic for both paper and live exits (2026-09-06,
    pulled out of each _apply_*_exit so the six-way rule -- liquidity,
    stop-loss, take-profit, profit-giveback, queue-pressure, stale -- lives
    in exactly one place). `address` (2026-09-17) is only used to key the
    stop-loss whipsaw guard's per-position confirmation tracking -- see
    _stop_loss_breach_started's own module-level comment. Liquidity is checked FIRST, ahead of everything
    else (2026-09-14, see bot/ai_review.py's
    AI-tunable strategy allowlist for the real
    incident this responds to): a genuinely dying pool doesn't care whether
    the position is currently up, down, or flat -- every price-based rule
    below is meaningless if there's nothing left to sell into by the time
    it fires. Everything else is checked in this order: stop-loss and
    take-profit are hard thresholds off the ENTRY price; profit-giveback is
    a softer,
    peak-relative check that only matters once a position has actually
    been in profit and is now reverting -- it can never fire while pnl_pct
    itself is <= 0, since "giving back profit" is meaningless on a
    position that has none. queue-pressure doesn't need a peak/reversal at
    all -- just enough profit plus a real backlog of candidates waiting
    for this slot (see _has_queue_pressure()). stale is checked LAST
    (lowest priority, every other rule gets first say) -- a pure time
    cutoff for a position that hasn't proven itself either way; it does
    NOT require any profit condition, unlike queue-pressure.

    Flat-coin grace period (2026-09-10, see config.py's flat_pnl_band_pct/
    flat_position_extra_secs docstrings for the fee-drag finding this
    responds to): when the stale clock would otherwise fire, a position
    sitting within flat_pnl_band_pct of 0% pnl gets up to
    flat_position_extra_secs of extra time instead of closing immediately
    -- it stays open and simply gets re-evaluated (including this same
    stale check, against the extended deadline) on the next cycle. A
    position that ISN'T flat is untouched by this -- it closes on the
    normal stale_position_max_age_secs clock exactly as before.

    Liquidity check DISABLED 2026-09-14 (user's explicit request, confirmed
    broken): real testing showed get_token_info()'s "liquidity" field reads
    near-zero for virtually every pre-migration bonding-curve token
    regardless of real health -- the check was firing within seconds on
    nearly every single buy. <= 0 disables it entirely, same convention
    every other disable-able threshold in this project already uses
    (trade_burst_limit, min_time_between_trades_secs,
    max_live_trades_per_session). Left in place (not deleted) since the
    surrounding design work (bounds, AI-tunable wiring) will likely be
    reused once a proper migrated-tokens-only version replaces this."""
    # Liquidity-drain signal (2026-09-19) -- see config.py's
    # liquidity_drain_mcap_threshold_usd/liquidity_drain_pct docstring for
    # the full reasoning (user's direct request to stop screening out
    # holder-concentration/sniper/bundler-heavy tokens and instead watch
    # for the pool actually draining once a position has real market cap
    # behind it). Checked first, ahead of even the absolute low-liquidity
    # floor below -- this catches a fast drain in progress, which is the
    # earlier, more useful warning than waiting for liquidity to cross a
    # fixed floor after most of it is already gone.
    liquidity_drain_mcap_threshold_usd = ai_review.get_effective_value("liquidity_drain_mcap_threshold_usd")
    liquidity_drain_pct = ai_review.get_effective_value("liquidity_drain_pct")
    if (liquidity_drain_mcap_threshold_usd > 0 and liquidity_drain_pct > 0
            and market_cap is not None and market_cap >= liquidity_drain_mcap_threshold_usd
            and liquidity is not None and peak_liquidity is not None and peak_liquidity > 0):
        drain_pct = (peak_liquidity - liquidity) / peak_liquidity * 100.0
        if drain_pct >= liquidity_drain_pct:
            _stop_loss_breach_started.pop(address, None)
            return "liquidity-drain"
    min_exit_liquidity = ai_review.get_effective_min_exit_liquidity_usd()
    if min_exit_liquidity > 0 and liquidity is not None and liquidity < min_exit_liquidity:
        return "low-liquidity"
    # Hard max-hold ceiling (2026-09-18) -- see config.max_hold_secs's own
    # docstring for the full reasoning/tradeoff. Checked before EVERYTHING
    # else below on purpose: this is a genuine "no matter what" cutoff,
    # unlike every other time-based rule in this function, which only
    # fires if nothing else already claimed the position.
    max_hold_secs = ai_review.get_effective_value("max_hold_secs")
    if max_hold_secs > 0 and age_secs >= max_hold_secs:
        _stop_loss_breach_started.pop(address, None)
        return "max-hold"
    stop_loss_pct = ai_review.get_effective_stop_loss_pct()
    take_profit_pct = ai_review.get_effective_take_profit_pct()
    if pnl_pct <= -stop_loss_pct:
        # Emergency-drawdown escape hatch (2026-09-17) -- checked BEFORE the
        # whipsaw confirmation below: a position already this far past its
        # stop-loss line is not noise, waiting to "confirm" it only makes
        # the loss worse. See config.emergency_drawdown_pct's own docstring
        # for the two real incidents (RIM, OMG) this responds to.
        emergency_drawdown_pct = ai_review.get_effective_value("emergency_drawdown_pct")
        if emergency_drawdown_pct > 0 and pnl_pct <= -emergency_drawdown_pct:
            _stop_loss_breach_started.pop(address, None)
            return "emergency-drawdown"
        # Whipsaw guard (2026-09-17) -- see _stop_loss_breach_started's own
        # module-level comment. Confirm the breach has held continuously
        # for stop_loss_confirm_secs before actually closing; a dip that
        # recovers before then never reaches this branch again (the "else"
        # below clears the watch entry the next time this function runs
        # for the same address and finds pnl_pct back above the line).
        stop_loss_confirm_secs = ai_review.get_effective_value("stop_loss_confirm_secs")
        if stop_loss_confirm_secs <= 0:
            return "stop-loss"
        first_breach = _stop_loss_breach_started.get(address)
        if first_breach is None:
            _stop_loss_breach_started[address] = time.time()
        elif time.time() - first_breach >= stop_loss_confirm_secs:
            return "stop-loss"
        # else: breaching, but not yet confirmed -- fall through to check
        # the other rules below (all of which require pnl_pct > 0 except
        # the losing-position/stale checks further down, which still apply
        # normally to a still-negative-but-unconfirmed position).
    else:
        _stop_loss_breach_started.pop(address, None)
    if pnl_pct >= take_profit_pct:
        return "take-profit"
    if peak_pnl_pct is not None and pnl_pct > 0:
        giveback_pct = ai_review.get_effective_profit_giveback_pct()
        if (peak_pnl_pct - pnl_pct) >= giveback_pct:
            return "profit-giveback"
    if has_queue_pressure and pnl_pct >= ai_review.get_effective_queue_pressure_min_profit_pct():
        return "queue-pressure"
    # Losing-position fast exit (2026-09-17, user + son's explicit request):
    # a position that's sideways or negative right now doesn't have to wait
    # for the full stale clock below -- checked here, ahead of that clock,
    # so it only ever makes losers/flats exit SOONER, never later. A
    # profitable-but-undecided position (pnl_pct > 0) skips this entirely
    # and keeps the normal stale_position_max_age_secs/flat-grace treatment.
    losing_position_max_age_secs = ai_review.get_effective_value("losing_position_max_age_secs")
    if pnl_pct <= 0 and losing_position_max_age_secs > 0 and age_secs >= losing_position_max_age_secs:
        return "stale-losing"
    stale_age = ai_review.get_effective_stale_position_max_age_secs()
    if age_secs >= stale_age:
        flat_band = ai_review.get_effective_flat_pnl_band_pct()
        flat_extra = ai_review.get_effective_flat_position_extra_secs()
        is_flat = abs(pnl_pct) <= flat_band
        if is_flat and flat_extra > 0 and age_secs < stale_age + flat_extra:
            return None  # flat but still within its grace period -- give it more time
        return "stale"
    return None


async def _fetch_price(address: str, symbol: str | None) -> tuple[str, float | None, float | None, float | None]:
    """Just the network fetch, no file writes -- safe to run many of these
    concurrently (see _check_positions_batched() below for why the file
    writes that use this result stay sequential instead).

    Rate-limit aware (2026-09-06, real incident): this loop's own price
    checks were independently hitting GMGN's rate limit with no backoff at
    all, contributing to the same ban bot/screener.py's checks hit --
    shares bot/rate_limiter.py's global state with that module rather than
    tracking it separately, since it's the same actual account/IP-level
    ban either loop can trigger or extend.

    Also computes market cap (2026-09-12, user's explicit request -- see
    paper_trader.update_last_price()'s own docstring for the full
    reasoning): get_token_info()'s response already includes both `price`
    and `total_supply` in the SAME call this function already makes for
    price alone -- market cap is just their product, no second API call.
    Verified live against a real trenches candidate's OWN reported
    market_cap field (price x total_supply matched it exactly: real
    example 4.344130589831702e-06 x 1,000,000,000 = 4344.13, exactly what
    GMGN itself reported) -- total_supply, NOT circulating_supply, is the
    field GMGN's own market_cap convention actually uses. Caught live
    before shipping: circulating_supply looked plausible (both fields are
    usually equal for a standard pump.fun 1B-supply token) but diverged
    hugely for one real test token, which would have shown a wildly wrong
    live figure inconsistent with entry_market_cap (itself pulled directly
    from trenches' own market_cap, i.e. this same price x total_supply
    convention) every time the two differ. None if either figure is
    missing/unparseable rather than guessing -- same fail-honest reasoning
    as the price side."""
    try:
        info = await asyncio.to_thread(gmgn.get_token_info, address, config.chain)
        rate_limiter.reset()
    except gmgn.GmgnRateLimitError as exc:
        log.warning("Exit check: price fetch for %s hit GMGN's rate limit.", symbol or address)
        await rate_limiter.handle_hit(exc.retry_after_secs)
        return address, None, None, None
    except gmgn.GmgnCliError:
        log.exception("Exit check: price fetch failed for %s -- skipping this cycle.", symbol or address)
        return address, None, None, None
    price = (info.get("price") or {}).get("price")
    price = float(price) if price else None
    market_cap = None
    if price is not None:
        try:
            supply = info.get("total_supply")
            if supply is not None:
                market_cap = price * float(supply)
        except (TypeError, ValueError):
            market_cap = None
    # Liquidity (2026-09-14, see bot/ai_review.py's
    # AI-tunable strategy allowlist) -- same field,
    # same call, no extra API cost. USD-denominated (verified live against
    # a real token: platform showed "Liq $4.02K", this field returned
    # 3919.82 -- same convention config.min_liquidity_usd's entry check
    # already uses), NOT SOL.
    liquidity = None
    try:
        raw_liquidity = info.get("liquidity")
        if raw_liquidity is not None:
            liquidity = float(raw_liquidity)
    except (TypeError, ValueError):
        liquidity = None
    return address, price, market_cap, liquidity


def _apply_paper_exit(address: str, pos: dict, current_price: float, market_cap: float | None,
                       has_queue_pressure: bool, liquidity: float | None = None) -> None:
    """The fast, local part -- file writes only, no network I/O. Run via
    asyncio.to_thread() from _check_positions_batched() (2026-10 latency
    audit) so its cross-process lock acquisition/file I/O can't stall the
    event loop, but still awaited one call at a time there, never
    concurrently, specifically because paper_trader's own
    update_last_price()/simulate_sell() each do a full read-modify-write
    of the SAME shared portfolio.json -- running these concurrently would
    race and silently lose updates, the exact bug this split avoids."""
    if not _is_price_plausible(pos.get("entry_price", 0.0), current_price, pos.get("symbol")):
        return
    peak_pnl_pct, peak_liquidity = paper_trader.update_last_price(address, current_price, market_cap, liquidity)
    pnl_pct = _pnl_pct(pos.get("entry_price", 0.0), current_price)
    age_secs = time.time() - pos.get("entry_ts", time.time())
    reason = _decide_exit_reason(address, pnl_pct, peak_pnl_pct, has_queue_pressure, age_secs, liquidity,
                                  market_cap, peak_liquidity)
    if reason is None:
        return
    try:
        closed = paper_trader.simulate_sell(address, reason, known_price=current_price)
        _stop_loss_breach_started.pop(address, None)
        log.warning("[bold yellow]PAPER EXIT (%s)[/bold yellow] %s pnl=%.2f%% (%.5f SOL)",
                    reason.upper(), closed.get("symbol"), closed.get("pnl_pct", 0.0) * 100, closed.get("pnl_sol", 0.0))
    except (paper_trader.PriceUnavailable, paper_trader.PositionError, paper_trader.ImplausiblePrice):
        # ImplausiblePrice should be rare here specifically -- this call
        # already passed _is_price_plausible() above using the same
        # current_price as known_price, so paper_trader.py's own copy of
        # the same check is normally redundant on THIS path. Caught
        # anyway for defense-in-depth (e.g. a future caller that skips
        # the pre-check) rather than letting it propagate uncaught.
        log.exception("Exit check: paper sell failed for %s -- will retry next cycle.", pos.get("symbol"))


def _apply_live_exit(address: str, pos: dict, current_price: float, market_cap: float | None,
                      has_queue_pressure: bool, liquidity: float | None = None) -> None:
    """Same reasoning as _apply_paper_exit() -- sequential on purpose,
    same shared-file race this whole split exists to avoid, and this one
    holds REAL funds so the stakes for a lost/racing update are higher,
    not lower. Same implausible-quote guard as the paper path too -- a
    live SWAP always executes at whatever the chain actually settles at
    regardless (real funds can't be "fooled" by a bad quote the way a
    recorded number can), but the DECISION to sell right now, on this
    quote, still shouldn't be made off a number that's almost certainly
    wrong."""
    entry_price = pos.get("entry_price")
    if not _is_price_plausible(entry_price, current_price, pos.get("symbol")):
        return
    peak_pnl_pct, peak_liquidity = executor.update_last_price(address, current_price, market_cap, liquidity)
    pnl_pct = _pnl_pct(entry_price, current_price)
    age_secs = time.time() - pos.get("entry_ts", time.time())
    reason = _decide_exit_reason(address, pnl_pct, peak_pnl_pct, has_queue_pressure, age_secs, liquidity,
                                  market_cap, peak_liquidity)
    if reason is None:
        return
    try:
        closed = executor.execute_sell(address, reason)
        _stop_loss_breach_started.pop(address, None)
        log.warning("[bold white on red]LIVE EXIT (%s)[/bold white on red] %s -- REAL FUNDS.",
                    reason.upper(), closed.get("symbol"))
    except executor.ExecutionBlocked:
        log.exception("Exit check: LIVE sell blocked for %s -- will retry next cycle.", pos.get("symbol"))
    except gmgn.GmgnCliError:
        # Real bug found live 2026-09-09 (same root cause as tui.py's own
        # matching fix): an uncaught GmgnCliError here (e.g. GMGN's swap
        # router returning "GetSwapRouteErr: little pool hit" for an
        # illiquid pool) was not caught by this function's own except
        # clause -- only ExecutionBlocked was. Same fail-open philosophy as
        # this module's own price-fetch failures (see module docstring):
        # a thin/broken pool must not crash the whole exit-check pass, and
        # this position stays open (nothing was sold on-chain, the failure
        # is at the route-quote stage) to retry on the next cycle -- which
        # will keep failing identically until the pool has real liquidity
        # again, so this WILL log repeatedly for a genuinely stuck position
        # rather than going silent, which is the honest thing to surface.
        log.exception("Exit check: LIVE sell for %s hit a GMGN routing/CLI error -- "
                       "position stays open, will retry next cycle.", pos.get("symbol"))


async def _check_positions_batched(positions: dict, apply_fn) -> None:
    """Fetches prices for up to config.exit_check_batch_size positions AT
    ONCE (asyncio.gather), then applies the resulting exits ONE AT A TIME
    (apply_fn is a plain sync function -- offloaded to a worker thread via
    asyncio.to_thread() and awaited immediately, 2026-10 latency audit, so
    it never stalls the event loop while it holds a cross-process file
    lock or does its read-modify-write of the shared portfolio file. Still
    strictly sequential -- each to_thread() call is awaited before the
    next starts -- so apply_fn's own same-shared-file race assumption
    (see its docstring) still holds; only WHERE it runs changed, not WHEN).
    Cuts a 60-position pass from ~28s (one at a time) down to roughly
    (60/batch_size) x one fetch's time -- see config.exit_check_batch_size's
    own comment for the real numbers this was measured against. Batched
    rather than fully concurrent (all positions at once) specifically to
    avoid bursting every price fetch simultaneously into GMGN's rate limit."""
    has_queue_pressure = await asyncio.to_thread(_has_queue_pressure)
    items = list(positions.items())
    for i in range(0, len(items), config.exit_check_batch_size):
        batch = items[i:i + config.exit_check_batch_size]
        results = await asyncio.gather(*[_fetch_price(addr, pos.get("symbol")) for addr, pos in batch])
        for (address, pos), (_, price, market_cap, liquidity) in zip(batch, results):
            if price is None:
                continue
            await asyncio.to_thread(apply_fn, address, pos, price, market_cap, has_queue_pressure, liquidity)


async def check_exits_once() -> None:
    paper_positions = dict(paper_trader.get_portfolio().get("positions", {}))
    await _check_positions_batched(paper_positions, _apply_paper_exit)

    live_positions = dict(executor.get_positions().get("positions", {}))
    await _check_positions_batched(live_positions, _apply_live_exit)


async def run_forever() -> None:
    log.info("Exit manager starting -- stop_loss=%.1f%% take_profit=%.1f%% interval=%.1fs "
              "(effective values, may already reflect an AI Free Will adjustment)",
              ai_review.get_effective_stop_loss_pct(), ai_review.get_effective_take_profit_pct(),
              config.exit_check_interval_secs)
    while True:
        try:
            await check_exits_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad pass must never kill this loop
            log.exception("Exit-check pass crashed -- continuing on the next poll.")
        await asyncio.sleep(config.exit_check_interval_secs)
