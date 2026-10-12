"""Unified trade-history log -- the single source of truth every closed
trade gets written to, paper AND live, regardless of exit reason
(stop_loss/take_profit/manual). Built 2026-09-06, user's own explicit
request ("build the watcher so it can save all trades... feed the AI
agent all that data for him to learn from it") specifically so the future
AI Free Will review loop has real, structured data to read.

Deliberately separate from paper_trader.py's own "closed" list
(logs/paper_portfolio.json) and executor.py's own "closed" list
(logs/live_positions.json) -- those stay exactly as they are, unchanged,
for each trader's own portfolio bookkeeping (open positions, running
realized PnL). This file exists purely so a learning loop can read ONE
consistent schema across both modes instead of reconciling two different
shapes itself.

Same atomic temp-file-then-os.replace pattern as every other staging file
in this project.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile

import bot.file_lock as file_lock
from config import config

log = logging.getLogger("gmgn-agent")


def _append_bounded(path: str, entry: dict, cap: int) -> None:
    """Shared append-one-record-atomically helper -- same temp-file-then-
    os.replace pattern as every other staging file in this project.
    Failures are logged and swallowed by the caller (record_trade()),
    never raised here."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    existing = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            log.exception("%s unreadable -- starting a fresh list rather than losing this record.", path)
            existing = []
    existing.append(entry)
    existing = existing[-cap:]
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    os.replace(tmp, path)


def record_trade(entry: dict) -> None:
    """Append one closed-trade record. Expected keys: mode ("paper"/
    "live"), address, symbol, entry_price, exit_price, amount_sol,
    pnl_sol, pnl_pct, reason, entry_ts, exit_ts -- callers build this dict
    themselves (paper_trader.py/executor.py already compute all of these
    for their own "closed" records; this just gets a copy). Never raises
    -- a failure here must not take down the trade that already
    completed, same reasoning as every other _append_* helper in this
    project.

    Writes to TWO files (2026-09-11, user's explicit request -- "build him
    actual access to his full trade history... make sure all trades are
    saved even on a dash refresh"): trade_history_file (bounded to 5000,
    the one a "clear the dash" reset archives and empties -- tui.py's live
    display reads this one) AND trade_history_master_file (bounded much
    higher at 50000, NEVER touched by a dash clear -- bot/ai_review.py
    reads THIS one via load_all_trades() below, so the AI's own review
    cadence and learning history survive every dashboard reset). Each
    write is independent and best-effort -- a failure on one must not
    block the other, same fail-soft reasoning as the rest of this
    function.

    Cross-process locked (2026-09-12, see config.py's trade_history_lock_file/
    trade_history_master_lock_file docstring) -- this was the one shared-
    state writer in the whole project missing the same protection every
    sibling file (paper_portfolio.json, live_positions.json, ai_memory.json,
    ai_overrides.json) already had, and a real ~17-hour gap in the master
    log was traced back to exactly that missing lock. Each file keeps its
    own separate lock, same as before -- a failure/slow lock on one still
    can't block the other."""
    try:
        with file_lock.cross_process_lock(config.trade_history_lock_file):
            _append_bounded(config.trade_history_file, entry, cap=5000)
    except Exception:
        log.exception("Failed to record trade-history entry for %s (dash file) -- "
                       "dashboard visibility only, the trade itself already completed regardless.",
                       entry.get("symbol"))
    try:
        with file_lock.cross_process_lock(config.trade_history_master_lock_file):
            _append_bounded(config.trade_history_master_file, entry, cap=50000)
    except Exception:
        log.exception("Failed to record trade-history entry for %s (master file) -- "
                       "AI learning-history visibility only, the trade itself already completed regardless.",
                       entry.get("symbol"))


def load_trades(limit: int | None = None) -> list[dict]:
    """Most-recent-last (same order as stored), from the DASH file
    (trade_history_file) -- this one gets cleared on a dashboard reset.
    Pass limit to get just the tail."""
    if not os.path.exists(config.trade_history_file):
        return []
    try:
        with open(config.trade_history_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        log.exception("Trade-history file unreadable.")
        return []
    return data[-limit:] if limit else data


def load_all_trades(limit: int | None = None) -> list[dict]:
    """Most-recent-last, from the MASTER file (trade_history_master_file)
    -- built 2026-09-11 so the AI Free Will review loop (bot/ai_review.py)
    can see real accumulated history that survives a dashboard reset,
    instead of load_trades()'s dash-file view which goes back to empty
    every time the dashboard is cleared. Falls back to load_trades() if
    the master file doesn't exist yet (e.g. right after this feature was
    added, before any new trade has been recorded to seed it) so there's
    no dead gap on first deploy."""
    if not os.path.exists(config.trade_history_master_file):
        return load_trades(limit)
    try:
        with open(config.trade_history_master_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        log.exception("Master trade-history file unreadable -- falling back to the dash file.")
        return load_trades(limit)
    return data[-limit:] if limit else data


def count_live_trades_since(since_ts: float) -> int:
    """How many LIVE (real-funds) trades have closed at or after
    since_ts -- built 2026-09-06 for the live-session auto-pause
    (config.max_live_trades_per_session). Counts every reason (stop-loss/
    take-profit/profit-giveback/queue-pressure/stale/manual) -- a manual
    close via tui.py's hotkeys still spends real funds just like an
    automatic one, so it counts the same toward the cap.

    Real bug fixed 2026-09-07: checked t.get("ts", 0) here, but every
    entry this file's own record_trade() actually writes only ever has
    "exit_ts" (see paper_trader.py's/executor.py's own closed-position
    dict shape) -- there is no top-level "ts" key at all, so this always
    silently defaulted to 0, meaning 0 >= since_ts was false for any real
    (positive) since_ts and the live-session auto-pause could NEVER
    trigger. Found by hand while pulling an unrelated hour-of-day
    breakdown for the user -- same missing-field mistake nearly produced
    a completely bogus report there too before being caught."""
    return sum(1 for t in load_trades() if t.get("mode") == "live" and t.get("exit_ts", 0) >= since_ts)
