"""Persisted trading-mode switch -- lets the dashboard flip between off/
paper/live without restarting any process. Same atomic temp-file-then-
os.replace pattern as bot/screener.py's other staging files, since this is
read by the dashboard API and (eventually) an executor loop, potentially at
the same instant a user clicks a toggle button.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time

import bot.ai_review as ai_review
import bot.file_lock as file_lock
import bot.trade_history as trade_history
from config import config, hour_in_windows

log = logging.getLogger("gmgn-agent")

VALID_MODES = ("off", "paper", "live")

# "live" is now settable (2026-09-05, bot/executor.py built) -- previously
# rejected here at this single choke point while real execution didn't
# exist. Switching to "live" does NOT itself place any trade -- it only
# changes what approve_candidate() does on the NEXT approval, and
# bot/executor.py's own _check_preconditions() still independently
# requires GMGN_ALLOW_AUTOMATED_TRADES=1 (GMGN's own separate CLI-level
# gate) before anything real can actually execute. Two gates, not one.
_SETTABLE_MODES = ("off", "paper", "live")


class InvalidMode(Exception):
    pass


def _default_state() -> dict:
    return {"mode": config.default_trading_mode, "updated_ts": time.time(), "note": None}


def get_state() -> dict:
    if not os.path.exists(config.trading_control_file):
        return _default_state()
    try:
        with open(config.trading_control_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("mode") not in VALID_MODES:
            return _default_state()
        return state
    except Exception:
        return _default_state()


def get_mode() -> str:
    return get_state()["mode"]


def set_mode(mode: str, note: str | None = None) -> dict:
    if mode not in _SETTABLE_MODES:
        raise InvalidMode(
            f"Cannot set trading mode to {mode!r} -- must be one of {_SETTABLE_MODES}."
        )
    state = {"mode": mode, "updated_ts": time.time(), "note": note}
    directory = os.path.dirname(config.trading_control_file) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, config.trading_control_file)
    return state


def is_live_session_paused() -> bool:
    """True once the effective max_live_trades_per_session live trades have
    closed since mode was last switched to live -- checked by
    bot/screener.py's _autonomous_buy() to block NEW live buys.

    Deliberately does NOT change `mode` away from "live" (2026-09-06, a
    real bug caught before it shipped): bot/executor.py's
    execute_sell()'s own _check_preconditions() requires mode == "live"
    for ANY close, automatic OR manual (tui.py's "x"/"s" hotkeys go
    through the same function). If this had switched mode to "paper"
    instead, any position still open at pause time would become
    permanently unsellable -- neither the exit rules nor the user could
    close it until mode was switched back to live. Blocking new BUYS
    while leaving mode alone means every exit path keeps working exactly
    as normal on whatever's still open.

    max_live_trades_per_session <= 0 disables this entirely (2026-09-09,
    user's explicit request -- guard added since this function previously
    had no disable path: count >= 0 would have paused immediately after
    the very first trade, the opposite of "off")."""
    max_live_trades_per_session = ai_review.get_effective_value(
        "max_live_trades_per_session")
    if max_live_trades_per_session <= 0:
        return False
    state = get_state()
    if state["mode"] != "live":
        return False
    count = trade_history.count_live_trades_since(state["updated_ts"])
    return count >= max_live_trades_per_session


def maybe_log_live_session_pause() -> None:
    """Real, code-enforced stop for a live test (2026-09-06, user's
    explicit request: "make sure it pauses for a break after 3 trades" --
    not something relying on catching the right moment manually). Logs
    once when the session first crosses the cap (tracked via a small
    in-state flag so this doesn't spam the log every cycle) -- the actual
    blocking happens in bot/screener.py's _autonomous_buy() via
    is_live_session_paused() above, called once per screening cycle."""
    if not is_live_session_paused():
        return
    state = get_state()
    if state.get("_session_pause_logged"):
        return
    count = trade_history.count_live_trades_since(state["updated_ts"])
    log.warning("[bold red]LIVE SESSION PAUSE[/bold red] %d live trade(s) closed since switching "
                "to live -- new live buys are now blocked. Mode stays 'live' so any position still "
                "open keeps its normal stop-loss/take-profit/manual-close protection; nothing new "
                "will be bought until mode is switched off and back to live again.", count)
    state["_session_pause_logged"] = True
    directory = os.path.dirname(config.trading_control_file) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, config.trading_control_file)


# -- Rolling trade-burst cooldown (2026-09-07) --------------------------------
# Separate file/state from the mode switch above -- this isn't "trading
# mode", it's a rolling counter, and giving it its own file means a corrupt/
# racy read of one can never affect the other. Same atomic temp-file-then-
# os.replace pattern as set_mode() above and every other staging file in
# this project. Persisted (not just in-memory) specifically so tui.py --
# a SEPARATE process that only ever reads JSON files, never touches the
# engine's live objects -- can show the cooldown countdown on the dashboard.

def _load_burst_state() -> dict:
    if not os.path.exists(config.trade_burst_state_file):
        return {"count": 0, "paused_until": 0.0}
    try:
        with open(config.trade_burst_state_file, "r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("count", 0)
        state.setdefault("paused_until", 0.0)
        return state
    except Exception:
        return {"count": 0, "paused_until": 0.0}


def _save_burst_state(state: dict) -> None:
    directory = os.path.dirname(config.trade_burst_state_file) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, config.trade_burst_state_file)


def record_trade_for_burst() -> None:
    """Call once per successful buy (paper OR live -- mode-agnostic on
    purpose, see config.py's trade_burst_limit docstring). Once the rolling
    count reaches config.trade_burst_limit, sets paused_until
    config.trade_burst_cooldown_secs into the future and resets count to 0
    -- the cooldown clears itself automatically once that time passes,
    no manual intervention needed, unlike max_live_trades_per_session's
    hard stop above.

    trade_burst_limit <= 0 disables this entirely (2026-09-08, user's
    explicit request -- unconfirmed but plausible theory that whatever
    candidates clear right as a cooldown lifts tend to already be past
    their peak, since a 10-buy burst usually means a frenzied stretch of
    launches, and the tokens still clearing right after are more likely
    riding the tail end of that same frenzy). Guarded explicitly rather
    than just setting the .env value to 0 and trusting the comparison
    below -- count >= 0 would otherwise trip a cooldown after every
    single trade, the opposite of disabled.

    Cross-process lock added 2026-09-10 (see config.py's
    trade_burst_state_lock_file docstring) -- same read-modify-write race
    class every other shared state file in this project already guards
    against."""
    trade_burst_limit = ai_review.get_effective_value("trade_burst_limit")
    trade_burst_cooldown_secs = ai_review.get_effective_value("trade_burst_cooldown_secs")
    if trade_burst_limit <= 0:
        return
    with file_lock.cross_process_lock(config.trade_burst_state_lock_file):
        state = _load_burst_state()
        state["count"] = state.get("count", 0) + 1
        if state["count"] >= trade_burst_limit:
            state["paused_until"] = time.time() + trade_burst_cooldown_secs
            state["count"] = 0
            log.warning("[bold yellow]TRADE BURST COOLDOWN[/bold yellow] %d trades in a row -- "
                        "pausing new buys for %.0fs. Exit management keeps running normally on "
                        "whatever's already open.", trade_burst_limit, trade_burst_cooldown_secs)
        _save_burst_state(state)


def is_burst_paused() -> bool:
    if ai_review.get_effective_value("trade_burst_limit") <= 0:
        return False
    return time.time() < _load_burst_state().get("paused_until", 0.0)


def burst_cooldown_remaining() -> float:
    return max(0.0, _load_burst_state().get("paused_until", 0.0) - time.time())


# -- Minimum time between trades (2026-09-09) -- shares the same state file
# as the burst cooldown above (a "last_trade_ts" field alongside "count"/
# "paused_until") -- both are trade-frequency throttles, no reason to split
# them into a second file. See config.py's min_time_between_trades_secs
# docstring for why this exists: entry-selectivity/exit thresholds don't
# control trade COUNT, only trade QUALITY, so fee drag from simply trading
# too often needed its own dedicated lever.

def record_trade_time(now: float | None = None) -> None:
    """Call once per successful buy (paper or live), same call sites as
    record_trade_for_burst() above. Same cross-process lock as that
    function, same reasoning (2026-09-10)."""
    with file_lock.cross_process_lock(config.trade_burst_state_lock_file):
        state = _load_burst_state()
        state["last_trade_ts"] = time.time() if now is None else now
        _save_burst_state(state)


def is_min_interval_active() -> bool:
    min_time_between_trades_secs = ai_review.get_effective_value("min_time_between_trades_secs")
    if min_time_between_trades_secs <= 0:
        return False
    last = _load_burst_state().get("last_trade_ts", 0.0)
    return (time.time() - last) < min_time_between_trades_secs


def min_interval_remaining() -> float:
    min_time_between_trades_secs = ai_review.get_effective_value("min_time_between_trades_secs")
    last = _load_burst_state().get("last_trade_ts", 0.0)
    return max(0.0, min_time_between_trades_secs - (time.time() - last))


# -- Time-of-day pause (2026-09-08) -- see config.py's time_pause_enabled/
# time_pause_windows_utc docstring for the real data (1,382 trades) this was
# built from. No persisted state needed here, unlike the two mechanisms
# above -- this is a pure function of the current clock, not something that
# gets "triggered" and needs to auto-clear later.

def is_time_paused(now: float | None = None) -> bool:
    if not ai_review.get_effective_value("time_pause_enabled"):
        return False
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    hour = dt.hour + dt.minute / 60.0
    return hour_in_windows(hour, ai_review.get_effective_value("time_pause_windows_utc"))
