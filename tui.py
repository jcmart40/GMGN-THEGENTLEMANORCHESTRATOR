"""Terminal control dashboard for the GMGN agent.

Mirrors Axiom-bot's own tui.py exactly in architecture and style (same Rich
Live/Layout screen, same msvcrt keyboard handling, same zero-confirmation
philosophy on hotkeys) -- built at the user's explicit request for something
"identical" to what Axiom-bot has. Runs as a SEPARATE process from
bot/screener.py (which, as of the 2026-09-06 fully-autonomous rebuild, is
GMGN-bot's own closest analog to Axiom's main.py -- see that module's own
docstring). This dashboard never touches the engine's live objects, it only
re-reads the same JSON files bot/screener.py/bot/exit_manager.py already
write to (activity_log.json, paper_portfolio.json, live_positions.json,
trading_control.json). A bug or crash here can't affect trading.

There is no approval queue anymore (2026-09-06, user's explicit direction:
fully autonomous, no human-in-the-loop per-trade approval, matching
Axiom-bot's own model) -- the engine buys the instant a candidate clears
security screening. This dashboard is purely observability + the two
things a human still controls: trading mode, and manually closing a
position early if desired.

Run:
    python tui.py

Hotkeys:
    s    Sell ALL open positions (paper AND live, whichever exist) --
         executes instantly on keypress, no confirmation prompt, same
         reasoning Axiom-bot's own "s" has: a confirmation dialog costs
         real seconds in a fast-moving market, exactly when this key is
         most likely to get used. Added 2026-09-06, user's explicit
         request for a real panic-sell button before a live test.
    1-9  Close the position at that row number in the Active Trades table
         -- ONE keypress, no typing a number and hitting Enter (2026-09-07,
         user's explicit request: "make it so it doesn't open another
         window for me to approve the sell... build me buttons I can
         push"). Replaces the old "x" + type-a-number prompt entirely.
         Exit management (bot/exit_manager.py) already auto-closes on
         stop-loss/take-profit; this is for closing one BEFORE that fires.
    o/k  SHUTDOWN -- Trading mode -> OFF. Both keys do the exact same
         thing (2026-09-07, user's explicit request for a real shutdown
         switch); "k" is here for the same muscle-memory reason Axiom-bot
         uses "k" for its own kill switch. Stops new buys only -- exit
         management keeps running normally on anything already open, same
         reasoning as the trade-burst-cooldown and live-session-cap below.
    r    Trading mode -> PAPER
    l    Trading mode -> LIVE. No confirmation prompt, matching Axiom's own
         "e"/"k" zero-confirmation convention for its highest-stakes keys --
         awareness comes from the header's bold mode badge, not a dialog.
    q    Quit the dashboard. bot/screener.py keeps running independently
         (same "main.py keeps running" message Axiom's own tui.py prints).
    v    Voice toggle (2026-10-15, bot/voice_io.py) -- arms/disarms the
         mic. Requires VOICE_ENABLED=true in .env; the Gentleman answers
         in speech and narrates closed trades whenever running. Voice
         commands never exceed the keyboard's authority, and money-
         touching ones must be repeated ("confirm") inside a short
         window -- the 2026-09-16 "l" incident is exactly why. Say
         "help" while armed for the full command list.

Keyboard input uses msvcrt (stdlib, Windows), same reasoning as Axiom-bot's
own tui.py: curses isn't available on stock Windows Python, and the
`keyboard` package needs an elevated global hook -- msvcrt is the only
builtin, unprivileged, non-blocking option.
"""
from __future__ import annotations

import json
import logging
import os
import time

import msvcrt
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import bot.ai_review as ai_review
import bot.executor as executor
import bot.gmgn_client as gmgn
import bot.paper_trader as paper_trader
import bot.trade_history as trade_history
import bot.trading_control as trading_control
from config import config

# Voice layer (bot/voice_io.py, 2026-10-15). Imported here ONLY as a
# cheap module import -- the heavy deps (faster-whisper, sounddevice,
# pyttsx3, numpy) load inside its worker threads at start(), so an
# import failure can't stop the dashboard booting. None until main()
# builds it (VOICE_ENABLED=true); every call site checks for None.
try:
    from bot.voice_io import VoiceIO
except Exception:  # noqa: BLE001 -- dashboard must still boot without voice deps
    VoiceIO = None

console = Console()
log = logging.getLogger("gmgn-agent")

_voice: "VoiceIO | None" = None
# Spoken-command confirmation state (money-touching intents only):
# (pending_intent, payload, expiry_ts). The operator must RE-SPEAK
# "confirm" before the expiry; any other utterance cancels instead of
# acting. tui.py-local rather than VoiceIO state on purpose: it gates
# trading actions, which by this file's own philosophy live on this side
# of the boundary, not the plumbing side.
_voice_pending: tuple[str, dict, float] | None = None
# Whether the operator's `v` voice SESSION is active (as opposed to the
# mic stream, which voice_io opens/closes per utterance underneath).
_voice_session = False

REFRESH_SECS = 0.5          # same rate Axiom's own tui.py settled on -- snappy
                            # for a human-watched dashboard without hammering
                            # the shared JSON files every frame.
STATUS_MSG_TTL_SECS = 6.0
BALANCE_TTL_SECS = 30.0    # wallet balance is a real RPC call -- throttled the
                           # same way Axiom-bot's own tui.py throttles its
                           # balance fetch, so this dashboard's own refresh
                           # rate can't spam it.
_balance_cache: tuple[float, float | None] = (0.0, None)  # (fetched_at, sol)


def _fetch_wallet_balance() -> float | None:
    """GMGN-bot has no RPC dependency anywhere else in this project
    (deliberately -- see bot/gmgn_client.py's own module docstring), but a
    plain SOL balance is public on-chain data any Solana RPC can answer, no
    API key needed -- using Solana Labs' own public endpoint here rather
    than adding a Helius dependency just for one display field."""
    global _balance_cache
    fetched_at, cached = _balance_cache
    if time.time() - fetched_at < BALANCE_TTL_SECS:
        return cached
    if not config.wallet_address:
        return None
    import urllib.request
    try:
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [config.wallet_address],
        }).encode()
        req = urllib.request.Request(
            "https://api.mainnet-beta.solana.com", data=payload, headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
        sol = resp["result"]["value"] / 1_000_000_000
        _balance_cache = (time.time(), sol)
        return sol
    except Exception:
        _balance_cache = (time.time(), cached)  # keep stale value rather than flash "unavailable"
        return cached


def _pnl_text(value: float, suffix: str = " SOL") -> Text:
    style = "bold green" if value >= 0 else "bold red"
    sign = "+" if value >= 0 else ""
    return Text(f"{sign}{value:.4f}{suffix}", style=style)


def _load_activity(limit: int = 10) -> list[dict]:
    if not os.path.exists(config.activity_log_file):
        return []
    try:
        with open(config.activity_log_file, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        return []
    return list(reversed(entries))[:limit]


# --- panels ------------------------------------------------------------------

def _header(status_msg: str) -> Panel:
    mode = trading_control.get_mode()
    mode_style = {"off": "bold yellow", "paper": "bold cyan", "live": "bold red"}[mode]
    title = Text.assemble(
        ("GMGN The Gentleman's Dashboard  ", "bold cyan"),
        (f"[{mode.upper()} MODE]", mode_style),
        ("   AUTONOMOUS — no approval gate", "dim"),
    )
    if mode == "live":
        title = Text.assemble(title, ("   REAL FUNDS", "bold white on red"))
    # Burst-cooldown badge (2026-09-07) -- surfaced right in the header, top
    # of the dashboard per the user's explicit request, not buried in a
    # panel further down, since it directly explains why buys have paused.
    remaining = trading_control.burst_cooldown_remaining()
    if remaining > 0:
        title = Text.assemble(
            title, ("   ", ""),
            (f"⏸ BURST COOLDOWN — {remaining:.0f}s remaining", "bold black on yellow"),
        )
    # Min-interval badge (2026-09-09 fix -- this throttle was built the same
    # night as the burst-cooldown one above with the identical "surface it
    # in the header, not buried" reasoning, but the header badge itself was
    # never actually added, found during a full-codebase audit). Separate
    # from the burst badge since the two can be active independently.
    min_interval_remaining = trading_control.min_interval_remaining()
    if min_interval_remaining > 0:
        title = Text.assemble(
            title, ("   ", ""),
            (f"⏸ MIN INTERVAL — {min_interval_remaining:.0f}s remaining", "bold black on cyan"),
        )
    # Voice badge (2026-10-15) -- same "surface it in the header, not
    # buried" convention as the two cooldown badges above: the operator
    # must always be able to see, at a glance, whether the mic is live.
    # Reads the SESSION flag, not voice_io's per-utterance listening state
    # -- that one flickers off between captures while the session is on,
    # which would make the badge blink confusingly.
    if _voice is not None:
        if _voice_session and _voice.listening:
            title = Text.assemble(title, ("   ", ""), ("● LISTENING", "bold black on green"))
        elif _voice_session:
            title = Text.assemble(title, ("   ", ""), ("● armed", "bold green"))
        elif _voice.stt_warming:
            title = Text.assemble(title, ("   ", ""), ("○ voice loading…", "dim"))
        elif _voice.stt_error:
            title = Text.assemble(title, ("   ", ""), ("× voice down", "bold red"))
        else:
            title = Text.assemble(title, ("   ", ""), ("○ voice idle (v)", "dim"))
    rest = Text(
        "   s = SELL ALL   1-9 = Close Position #   o/k = SHUTDOWN   r/l = Mode Paper/Live   "
        + ("v = Voice   " if _voice is not None else "")
        + "q = Quit   ",
        style="dim",
    )
    sub = Text(status_msg, style="bold cyan") if status_msg else rest
    return Panel(Text.assemble(title, "\n", sub), border_style="cyan")


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


# Short-lived "ghost" cache (2026-09-13) -- see _open_position_entries()'s
# own docstring below for why this exists. addr -> (kind, last_known_pos,
# last_seen_ts).
_position_ghost_cache: dict[str, tuple[str, dict, float]] = {}
_GHOST_GRACE_SECS = 20.0


def _open_position_entries() -> list[tuple[str, str, dict]]:
    """Real open positions from both traders' own files, plus a short-lived
    'ghost' entry for anything that just vanished from one of those files
    without a matching close showing up yet.

    bot/executor.py's execute_sell() (and paper_trader.py's simulate_sell())
    deliberately POP a position out of its own file the instant a sell
    attempt starts, before the slow network calls that follow
    (get_wallet_holdings/submit_swap) -- see executor.py's own docstring,
    built 2026-09-09 after a real lost-position bug. If that attempt then
    fails, the position gets restored just as fast. Real and correct
    behavior, but confusing to a human watching live: 2026-09-13, this
    dashboard was seen to show an open live position (deep in profit,
    already past its take-profit target) simply vanish for a render or
    two, then reappear -- nothing was actually lost or sold, but it looked
    exactly like a bug, and the aggregate Total PnL visibly dropped by that
    position's whole unrealized amount in the meantime. This bridges that
    gap by showing the last-known row instead of blanking it during that
    few-second in-between window -- it does NOT touch the actual trading
    logic that causes the gap, only how the dashboard reacts to it.

    Used by BOTH _combined_open_positions() (the visual table + PnL math)
    and _open_position_rows() (1-9 hotkey targeting) so their row order can
    never drift apart -- see _open_position_rows()'s own docstring for why
    that matters. A hotkey press that lands on a ghost row harmlessly fails
    with "no open position" from _close_one() (the position genuinely isn't
    in the file at that instant) rather than closing the wrong real row."""
    now = time.time()
    seen: set[str] = set()
    entries: list[tuple[str, str, dict]] = []
    for addr, pos in paper_trader.get_portfolio().get("positions", {}).items():
        entries.append(("paper", addr, pos))
        _position_ghost_cache[addr] = ("paper", pos, now)
        seen.add(addr)
    for addr, pos in executor.get_positions().get("positions", {}).items():
        entries.append(("live", addr, pos))
        _position_ghost_cache[addr] = ("live", pos, now)
        seen.add(addr)

    # A position that just genuinely closed should stop being shown as a
    # ghost immediately, not wait out the grace period below.
    closed_addrs = {c.get("address") for c in paper_trader.get_portfolio().get("closed", [])[-20:]}
    closed_addrs |= {c.get("address") for c in executor.get_positions().get("closed", [])[-20:]}

    for addr in list(_position_ghost_cache):
        if addr in seen:
            continue
        kind, pos, last_seen = _position_ghost_cache[addr]
        if addr in closed_addrs or now - last_seen > _GHOST_GRACE_SECS:
            del _position_ghost_cache[addr]  # genuinely closed, or a stale ghost past the safety-net TTL
            continue
        entries.append((kind, addr, pos))
    return entries


def _combined_open_positions() -> list[tuple[str, str, dict]]:
    return _open_position_entries()


def _fee_adjusted_pnl_pct(mode: str, entry: float, last: float) -> float:
    """Raw (last-entry)/entry, with paper_simulated_fee_pct subtracted --
    for PAPER positions since 2026-09-12 (user's explicit request: "add
    those fees to show in the actual price in Unrealized so it can show
    the actual gains before we close the position" -- real confusion
    found live: the old raw-only display showed a paper position like
    baton as +8.96% while it was open, but it actually closed at -2.54%
    the instant the ~11.5% simulated round-trip fee got applied), and for
    LIVE positions too as of 2026-09-13 (user's explicit request for "a
    rough visual of what we are losing and profiting even if is a rough
    estimate"). Live is genuinely fee-blind while a position is still
    OPEN, same root problem as paper: both entry_price (bot/executor.py's
    execute_buy(), a pre-swap quote) and the live last_price used here
    (a periodic quote, not a sell) are un-fee-adjusted raw numbers -- the
    real fee only actually lands in the settled price at the moment a
    live position actually CLOSES via a real executed swap. So this is a
    deliberate estimate for the OPEN/unrealized display only, using the
    same measured ~11.5% round-trip figure as paper since there's no
    separate measured live-only figure -- it does NOT touch realized PnL
    on already-closed live trades, which stays exact (that already comes
    from the real settled swap price, nothing to adjust). Returns a
    fraction (e.g. 0.05 = 5%), not a percentage -- multiply by 100 for
    display, by amount_sol for a SOL figure, same convention
    _pnl_pct()-family functions elsewhere use."""
    if not entry or not last:
        return 0.0
    raw = (last - entry) / entry
    return raw - config.paper_simulated_fee_pct


def _fmt_market_cap(value: float | None) -> str:
    """$8.2k / $1.2M style, matching how every trader actually talks about
    a token's market cap -- not a raw dollar figure. Added 2026-09-12,
    same request as _positions_table()'s own docstring."""
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"${value/1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value/1_000:.1f}k"
    return f"${value:.0f}"


def _unrealized_pnl_sol(rows: list[tuple[str, str, dict]]) -> float:
    """Reads the last_price bot/exit_manager.py caches onto each position
    (added 2026-09-06 specifically for this) -- 0.0 for a position that
    hasn't had an exit-check cycle yet, same as Axiom-bot's own dashboard
    showing 0 unrealized PnL the instant after a fresh buy. Fee-adjusted
    (2026-09-12) via _fee_adjusted_pnl_pct() above -- see that function's
    own docstring for why paper and live are treated differently here."""
    total = 0.0
    for mode, _, pos in rows:
        entry, last = pos.get("entry_price"), pos.get("last_price")
        total += pos.get("amount_sol", 0.0) * _fee_adjusted_pnl_pct(mode, entry, last)
    return total


def _pnl_panel() -> Panel:
    """Matches Axiom-bot's own "Live PnL" panel exactly: TOTAL/Realized/
    Unrealized PnL, Win Rate, Trades, and one more figure in the 6th slot
    -- Axiom shows Tips Paid there (a real, separate fee cost); GMGN-bot
    has no equivalent per-trade fee figure yet (fees are baked into
    GMGN's own reported price, not itemized -- see the real 11-12%
    round-trip fee data captured 2026-09-06), so Open Positions goes there
    instead, a number this dashboard doesn't show anywhere else.

    Reads bot/trade_history.py's unified log (capped at 5000) rather than
    combining paper_trader.get_portfolio()["closed"] + executor.get_positions()
    ["closed"] the way this function originally did -- real bug found live
    2026-09-10: those two files are each independently capped at only 1000
    entries (see paper_trader.py's/executor.py's own simulate_sell()/
    execute_sell()), while trade_history.json is capped at 5000 and is what
    _exit_reason_panel() below already correctly reads. Once a session ran
    well past 1000 closed trades, this panel's Trades/Win Rate/Realized PnL
    were silently clipped to only the most recent ~1000 while the exit-reason
    breakdown panel right below it kept counting up to 5000 -- two panels on
    the same screen quietly disagreeing with each other and with the true
    session total, exactly the kind of "something looks off" a user would
    notice without being able to pin down why."""
    all_closed = [t for t in trade_history.load_trades() if t.get("pnl_sol") is not None]
    wins = sum(1 for c in all_closed if c["pnl_sol"] > 0)
    closed = len(all_closed)
    realized = sum(c["pnl_sol"] for c in all_closed)

    open_rows = _combined_open_positions()
    unrealized = _unrealized_pnl_sol(open_rows)
    total_pnl = realized + unrealized
    win_rate = (wins / closed * 100) if closed else 0.0

    grid = Table.grid(expand=True)
    for _ in range(6):
        grid.add_column(justify="center")
    grid.add_row(
        Text.assemble(("TOTAL PnL\n", "dim"), _pnl_text(total_pnl)),
        Text.assemble(("Realized PnL\n", "dim"), _pnl_text(realized)),
        Text.assemble(("Unrealized\n", "dim"), _pnl_text(unrealized)),
        Text.assemble(("Win Rate\n", "dim"), Text(f"{win_rate:.0f}% ({wins}/{closed})", style="bold")),
        Text.assemble(("Trades\n", "dim"), Text(f"{closed}", style="bold cyan")),
        Text.assemble(("Open Positions\n", "dim"), Text(f"{len(open_rows)}", style="bold magenta")),
    )
    return Panel(grid, title="Live PnL", border_style="green")


# Adapted from Axiom-bot's own "Closed Trades by Bucket" (Sniper/Momentum/
# Copy-Trade/Manual) -- GMGN-bot had no bucket concept (one screening
# strategy, not several) until bot/wallet_follow.py (2026-09-15), so the
# breakdown here has always been by EXIT REASON instead, using
# bot/trade_history.py's own "reason" field. See _source_panel() below for
# the genuinely analogous "which signal found this trade" breakdown, now
# that there's more than one signal source -- deliberately a SEPARATE,
# reporting-only panel: every signal source still shares the exact same
# trading/exit logic (user's explicit architecture call, one unified
# engine), this table exists purely so performance by source is visible,
# not because sources are managed any differently.
#
# Updated 2026-09-10 (found stale during a full-codebase audit): this list
# predates the 2026-09-06 exit_manager.py additions of profit-giveback/
# queue-pressure/stale -- those three still rendered correctly (the
# fallback below appends any reason not already in this list), just not in
# a deliberately chosen position. Listed in exit_manager.py's own
# _decide_exit_reason() priority order.
#
_EXIT_REASON_ORDER = ["stop-loss", "take-profit", "profit-giveback"]

# "stale" and "manual" hidden from this panel entirely (2026-09-15, user's
# explicit request: "remove this ones... because I can read all his active
# trades"). Deliberately a HARD exclusion in _exit_reason_stats() below,
# not just removed from _EXIT_REASON_ORDER above -- real "stale"/"manual"
# closed trades exist right now, and the panel's own untracked-reason
# fallback (same pattern _source_panel() uses for a paused signal source)
# would otherwise just re-surface them anyway, since that fallback exists
# specifically to protect against silently losing DATA, which isn't the
# same case as a user deliberately not wanting a category shown at all.
# NOT deleted from trade_history.json/exit_manager.py's own exit logic --
# these trades still happened and still count in overall PnL, just not
# broken out on this specific panel.
#
# "skyeye_copy_exit" added to this same exclusion (2026-09-15, later same
# day) -- originally left visible here on purpose (a different breakdown
# dimension than the source panel, exit-reason vs who-found-it), but the
# user directly flagged the real consequence: it's a frequent, distinct
# reason (skyeye_copy trades a lot) that was forcing breakdown_row's own
# dynamic height up an extra row, eating into the positions table's space
# -- the exact thing the dynamic-height fix was built to protect. Same
# reasoning as stale/manual: still a real, active reason (not paused),
# so this has to be a hard exclusion too, or the panel's own fallback
# would immediately re-surface it.
#
# "queue-pressure" added to this same exclusion (2026-09-15, later same
# day still) -- user's explicit request: "take that one off the screen
# but don't disable it just off the screen there just blocking the view
# of the active trades." Same reasoning as the others: genuinely active
# (bot/exit_manager.py's own queue-pressure exit logic keeps running
# completely untouched, real queue-pressure exits still happen and still
# count in PnL), just no longer taking up a row on this specific panel.
# ALSO removed from _EXIT_REASON_ORDER above, not just added here -- that
# list alone would still leave a zero-row placeholder for it even with
# the hard exclusion below, since _exit_reason_stats() pre-populates
# every _EXIT_REASON_ORDER entry before the exclusion check ever runs.
#
# "emergency-drawdown" added to this same exclusion (2026-09-19) --
# re-enabled after a real live trade (StepFun) rode a fast crash from
# +9.86% peak down to -37.7% raw before the stop-loss whipsaw guard's
# 10-second confirmation window finally caught it, with emergency_drawdown_pct
# disabled (an earlier isolation test that never got turned back on). User's
# explicit request re-enabling it: keep the real protection, but don't add
# a new row to this panel -- same "don't put it back up on the dashboard,
# I won't be able to see the trades" concern the queue-pressure/skyeye_copy_exit
# exclusions above were built for. Real emergency-drawdown exits still
# happen in bot/exit_manager.py, still count in overall PnL and
# trade_history.json, just not broken out here.
_EXCLUDED_EXIT_REASONS = {"stale", "manual", "skyeye_copy_exit", "queue-pressure", "emergency-drawdown"}


def _exit_reason_stats() -> list[tuple[str, int, int, float]]:
    stats: dict[str, list] = {r: [0, 0, 0.0] for r in _EXIT_REASON_ORDER}
    for t in trade_history.load_trades():
        if t.get("pnl_sol") is None:
            continue
        reason = t.get("reason", "manual")
        if reason in _EXCLUDED_EXIT_REASONS:
            continue
        s = stats.setdefault(reason, [0, 0, 0.0])
        if t["pnl_sol"] > 0:
            s[0] += 1
        elif t["pnl_sol"] < 0:
            s[1] += 1
        s[2] += t["pnl_sol"]
    order = _EXIT_REASON_ORDER + [r for r in stats if r not in _EXIT_REASON_ORDER]
    return [(r, *stats[r]) for r in order]


def _exit_reason_panel() -> Panel:
    table = Table(expand=True, header_style="bold magenta", box=None, show_edge=False)
    table.add_column("Exit Reason")
    table.add_column("Win Rate", justify="right")
    table.add_column("Net PnL", justify="right")
    for reason, wins, losses, net_pnl in _exit_reason_stats():
        closed = wins + losses
        win_rate = f"{100 * wins / closed:.0f}% ({wins}/{closed})" if closed else "—"
        table.add_row(reason.replace("-", " ").title(), win_rate, _pnl_text(net_pnl))
    return Panel(table, title="Closed Trades by Exit Reason", border_style="cyan")


# Which signal found the trade (2026-09-15, bot/wallet_follow.py) -- same
# breakdown pattern as _exit_reason_panel() above, different grouping
# field. "screening" listed first since it's the original, default source;
# any future signal source (Dev Snipe, Token Snipe, ...) just needs to tag
# its own candidates with "source" and it appears here automatically, no
# panel changes needed.
# wallet_follow deliberately excluded (2026-09-15, paused the same day --
# see .env's own WALLET_FOLLOW_ENABLED comment for the full reasoning:
# Cupsey's trade frequency never resolved the latency-race concern that
# already disabled this piece once for Cented, so it's parked in favor of
# SnipeX tracking him instead). NOT deleted from the codebase, just no
# longer a standing row on this dashboard while paused -- _source_stats()'s
# own fallback still surfaces it as an extra row if any wallet_follow-
# sourced trade is ever actually present in trade_history.json, so real
# data is never silently hidden, only the empty placeholder row while
# genuinely inactive.
#
# snipex ALSO excluded now (2026-09-15, later the same day, paused too --
# see .env's own SNIPEX_ENABLED comment for the full reasoning: the
# user's own real-time awareness of Cupsey beats SnipeX's 45s LLM-call
# poll cycle, so the automated watch was adding latency without adding a
# real speed advantage -- "I need speed not something dragging the
# gentleman down"). Same soft-removal treatment as wallet_follow above,
# for the identical reason: genuinely paused, not actively generating new
# data, so the panel's own fallback staying in place is correct and safe.
_SOURCE_ORDER = ["screening", "dev_snipe", "token_snipe"]

# skyeye_copy hidden from this panel too (2026-09-15, user's explicit
# request, purely to free vertical space for the positions table below --
# "we are not losing it it's just not going to be on the dash"). UNLIKE
# wallet_follow above, this is a HARD exclusion (see _EXCLUDED_SOURCES
# below), not just removal from _SOURCE_ORDER -- skyeye_copy is still
# fully ACTIVE and generating real trades right now (unlike the genuinely
# paused wallet_follow), so the panel's own untracked-source fallback
# would otherwise immediately re-surface it anyway, exactly like "stale"/
# "manual" had to become a hard exclusion on the exit-reason panel above
# for the identical reason. skyeye_copy.py itself is completely untouched
# and keeps trading normally -- only this panel's display is affected.
_EXCLUDED_SOURCES = {"skyeye_copy"}


def _source_stats() -> list[tuple[str, int, int, float]]:
    stats: dict[str, list] = {s: [0, 0, 0.0] for s in _SOURCE_ORDER}
    for t in trade_history.load_trades():
        if t.get("pnl_sol") is None:
            continue
        source = t.get("source", "screening")
        if source in _EXCLUDED_SOURCES:
            continue
        s = stats.setdefault(source, [0, 0, 0.0])
        if t["pnl_sol"] > 0:
            s[0] += 1
        elif t["pnl_sol"] < 0:
            s[1] += 1
        s[2] += t["pnl_sol"]
    order = _SOURCE_ORDER + [s for s in stats if s not in _SOURCE_ORDER]
    return [(s, *stats[s]) for s in order]


def _source_panel() -> Panel:
    table = Table(expand=True, header_style="bold magenta", box=None, show_edge=False)
    table.add_column("Signal Source")
    table.add_column("Win Rate", justify="right")
    table.add_column("Net PnL", justify="right")
    for source, wins, losses, net_pnl in _source_stats():
        closed = wins + losses
        win_rate = f"{100 * wins / closed:.0f}% ({wins}/{closed})" if closed else "—"
        table.add_row(source.replace("_", " ").title(), win_rate, _pnl_text(net_pnl))
    return Panel(table, title="Closed Trades by Signal Source", border_style="cyan")


def _positions_table() -> Panel:
    """Same column set as Axiom-bot's own Active Trades table (Token/
    Amount/Entry/Current Price/Unreal. PnL/% PnL/Duration), with one
    addition -- a Mode column -- since GMGN-bot can hold paper AND live
    positions open at once, unlike Axiom-bot where the whole process is
    one mode at a time. "Market Cap" (Axiom's own 5th column) was
    originally dropped (not cached per-position here, unlike price) but
    added back 2026-09-12 at the user's explicit request ("so I can see
    where the market cap was when he buys a token"), placed right next to
    Current Price per the user's own placement request -- shows entry ->
    live so growth is visible at a glance, not just the current figure."""
    table = Table(expand=True, header_style="bold magenta")
    table.add_column("#", justify="center", style="bold yellow")
    table.add_column("Token", overflow="fold")
    table.add_column("Mode", justify="center")
    table.add_column("Amount", justify="right")
    table.add_column("Entry", justify="right")
    table.add_column("Current Price", justify="right")
    table.add_column("Mkt Cap (entry → live)", justify="right")
    table.add_column("Unreal. PnL", justify="right")
    table.add_column("% PnL", justify="right")
    table.add_column("Duration", justify="right")

    rows = _combined_open_positions()
    if not rows:
        table.add_row("", "[dim]— no active trades —[/dim]", "", "", "", "", "", "", "", "")
    else:
        now = time.time()
        # Row number here is what 1-9 hotkeys map to -- MUST stay in the
        # same order _open_position_rows() below builds its list in (paper
        # positions first, then live), or a keypress would close the wrong
        # position. 9-row cap noted, not enforced: max_concurrent_positions
        # keeps real position counts far below this in practice.
        for i, (kind, addr, pos) in enumerate(rows, start=1):
            entry, last = pos.get("entry_price"), pos.get("last_price")
            amount = pos.get("amount_sol", 0.0)
            # Fee-adjusted for paper positions (2026-09-12, see
            # _fee_adjusted_pnl_pct()'s own docstring) -- shows what you'd
            # ACTUALLY walk away with if closed right now, not the raw
            # price move with the ~11.5% fee still invisibly pending.
            fee_adj_frac = _fee_adjusted_pnl_pct(kind, entry, last)
            unreal_pnl = amount * fee_adj_frac
            unreal_pct = fee_adj_frac * 100
            held = max(0.0, now - pos.get("entry_ts", now))
            entry_mc = _fmt_market_cap(pos.get("entry_market_cap"))
            live_mc = _fmt_market_cap(pos.get("last_market_cap"))
            table.add_row(
                str(i) if i <= 9 else "—",
                pos.get("symbol") or addr[:8],
                Text("PAPER", style="cyan") if kind == "paper" else Text("LIVE", style="bold red"),
                f"{amount:.3f} SOL",
                f"{entry:.10f}" if entry else "—",
                f"{last:.10f}" if last else "—",
                f"{entry_mc} → {live_mc}",
                _pnl_text(unreal_pnl),
                _pnl_text(unreal_pct, suffix="%"),
                _fmt_duration(held),
            )
    return Panel(table, title="Active Trades", border_style="blue")


def _status_panel() -> Panel:
    """Matches Axiom-bot's own "System Status • Risk Settings" 3x3 grid,
    adapted field-for-field to what actually applies to GMGN-bot: no RPC
    (GMGN's own platform is the dependency instead -- see bot/gmgn_client.py's
    own docstring for why there's no Helius/RPC anywhere in this project),
    one stop-loss/take-profit tier instead of Axiom's general+sniper split
    (GMGN-bot has one strategy, not several buckets), and an explicit
    Holder-Count Filter status since that's a real, user-requested toggle
    now (removed 2026-09-06) worth surfacing rather than leaving invisible."""
    mode = trading_control.get_mode()
    mode_style = {"off": "bold yellow", "paper": "bold cyan", "live": "bold red"}[mode]
    status_text = Text(mode.upper(), style=mode_style)

    if mode == "live":
        bal = _fetch_wallet_balance()
        bal_text = Text(f"{bal:.4f} SOL" if bal is not None else "unavailable",
                         style="bold" if bal is not None else "dim")
    else:
        bal_text = Text(f"{mode.upper()} MODE", style="dim")

    platform_text = Text.assemble(("GMGN API", "bold green"), (f"  (chain: {config.chain})", "dim"))
    slip_text = Text(f"{config.slippage_pct:.1f}%", style="bold")
    size_text = Text(f"{config.trade_size_sol:.3f} SOL", style="bold")

    sl, tp = ai_review.get_effective_stop_loss_pct(), ai_review.get_effective_take_profit_pct()
    tpsl_text = Text.assemble(
        ("TP ", "dim"), (f"{tp:.1f}%", "bold green"),
        ("   SL ", "dim"), (f"{sl:.1f}%", "bold red"),
    )

    fee_text = Text(f"{config.priority_fee_sol:.5f} SOL", style="bold")
    lesson_count = len(ai_review.load_memory().get("lessons_learned", []))
    ai_text = Text(f"{lesson_count} lesson(s) learned", style="bold magenta")
    holder_text = Text("OFF — trades any holder count", style="bold yellow")

    grid = Table.grid(expand=True)
    for _ in range(3):
        grid.add_column(justify="center")
    grid.add_row(
        Text.assemble(("Trading Status\n", "dim"), status_text),
        Text.assemble(("Wallet Balance\n", "dim"), bal_text),
        Text.assemble(("Platform\n", "dim"), platform_text),
    )
    grid.add_row(
        Text.assemble(("Slippage\n", "dim"), slip_text),
        Text.assemble(("Trade Size\n", "dim"), size_text),
        Text.assemble(("Stop Loss / Take Profit\n", "dim"), tpsl_text),
    )
    grid.add_row(
        Text.assemble(("Priority Fee\n", "dim"), fee_text),
        Text.assemble(("AI Free Will\n", "dim"), ai_text),
        Text.assemble(("Holder-Count Filter\n", "dim"), holder_text),
    )
    return Panel(grid, title="System Status  •  Risk Settings", border_style="yellow")


def _load_buy_attempts(limit: int = 10) -> list[dict]:
    """Reads cleared_candidates.json -- every buy ATTEMPT and its real
    outcome (bought/paper_bought/buy_failed/skipped_*), including WHY a
    live buy failed (rate limit, blocked, etc). Added 2026-09-06: this
    data existed the whole time but the dashboard never showed it at
    all -- during tonight's live test, every single buy was failing on
    GMGN's rate limit and nothing on screen reflected that. Merged into
    the SAME activity panel below rather than a new one -- user's explicit
    earlier feedback was "don't add new windows to the dashboard.\""""
    if not os.path.exists(config.cleared_candidates_file):
        return []
    try:
        with open(config.cleared_candidates_file, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        return []
    return list(reversed(entries))[:limit]


def _activity_panel() -> Panel:
    """Merges screening decisions (activity_log.json) AND buy attempts/
    outcomes (cleared_candidates.json) into ONE panel, sorted by time --
    same existing panel, not a new window, just no longer blind to
    whether a cleared candidate's buy actually succeeded."""
    screening = [{"kind": "screen", **e} for e in _load_activity(15)]
    buys = [{"kind": "buy", **e} for e in _load_buy_attempts(15)]
    combined = sorted(screening + buys, key=lambda e: e.get("ts", 0), reverse=True)[:12]

    if not combined:
        body = Text("— no activity logged yet —", style="dim")
    else:
        lines = []
        for e in combined:
            if e["kind"] == "screen":
                verdict = e.get("verdict", "?")
                style = "bold green" if verdict == "cleared" else "dim"
                detail = "; ".join(e.get("reasons") or [])
                tag = verdict.upper()
            else:
                status = e.get("status", "?")
                style = {
                    "bought": "bold red", "paper_bought": "bold cyan",
                    "buy_failed": "bold yellow", "skipped_off": "dim",
                    "skipped_max_positions": "dim",
                }.get(status, "dim")
                error = e.get("error") or ""
                if "rate limit" in error.lower():
                    detail = "RATE LIMITED (GMGN platform-side ban)"
                else:
                    detail = error[:60] if status == "buy_failed" else ""
                tag = status.upper()
            lines.append(Text.assemble(
                (f"[{tag:>20}] ", style),
                (f"{e.get('symbol') or '?':<12} ", "bold"),
                (detail, "dim"),
            ))
        body = Text("\n").join(lines)
    return Panel(body, title="Recent Activity (screening + buy attempts)", border_style="blue")


def render(status_msg: str = "") -> Layout:
    layout = Layout()
    breakdown_row = Layout()
    # Side by side, not stacked (2026-09-15) -- adding _source_panel() as
    # its own full-width row would push every panel below it further down
    # the screen; splitting this one row horizontally instead keeps the
    # dashboard the same overall height.
    breakdown_row.split_row(
        Layout(_exit_reason_panel()),
        Layout(_source_panel()),
    )
    # Height computed fresh every render (2026-09-15), not a hardcoded
    # constant -- real history behind this, worth keeping: this was 6
    # (fit only the exit-reason panel, before _source_panel() existed),
    # then a static 9 (worst case for 6 signal sources), then a static 8
    # after wallet_follow/stale/manual were excluded -- each fix was
    # already stale by the time the NEXT row got added or removed,
    # because a fixed number can only ever be right for one exact
    # snapshot of active sources/reasons. Real bug this caused: at 6 it
    # silently clipped _source_panel()'s lower rows off the bottom
    # instead of erroring -- the data was always correct, just not fully
    # visible, only found because the user reported it directly. The
    # user then separately noticed genuine SLACK once both panels
    # happened to only need 4 rows each, and asked to reclaim it -- but
    # shrinking to a new fixed 7 would just reintroduce the exact same
    # clipping bug the next time skyeye_copy_exit (or any other
    # currently-quiet reason/source) reappears. Computing the real
    # current row count here, every render, is the actual fix for both
    # problems at once -- always exactly enough space, never wasted,
    # self-adjusting as sources/reasons come and go, no more manual
    # re-tuning needed for whatever gets added or removed next.
    breakdown_rows_needed = max(len(_exit_reason_stats()), len(_source_stats()))
    breakdown_row_height = 2 + 1 + breakdown_rows_needed  # border + table header + data rows
    # Real bug found 2026-09-17: the dynamic sizing above (correctly) fixed
    # the breakdown panel's OWN clipping, but "positions" below it had no
    # size or minimum_size at all -- an unsized Layout just gets whatever's
    # left after every sized sibling is allocated, so as more distinct exit
    # reasons/sources accumulated over time (stale-losing, low-liquidity,
    # emergency-drawdown all added the same day) and breakdown_row_height
    # grew accordingly, the OPEN POSITIONS table kept shrinking to make
    # room -- reported live as "I can't see any of the trades." A
    # minimum_size floor here means positions degrades gracefully (fewer
    # rows visible on a short terminal) instead of being squeezed toward
    # zero by however big the breakdown panel happens to need to be.
    layout.split_column(
        Layout(_header(status_msg), size=4),
        Layout(_pnl_panel(), size=4),
        Layout(breakdown_row, size=breakdown_row_height),
        Layout(_positions_table(), name="positions", minimum_size=8),
        Layout(_status_panel(), size=8),
        Layout(_activity_panel(), size=10),
    )
    return layout


# --- keyboard controls ---------------------------------------------------------

def _open_position_rows() -> list[tuple[str, str]]:
    """(kind, address) pairs, in the exact same order _combined_open_positions()
    shows in the Active Trades table -- both now derive from the same
    _open_position_entries() (2026-09-13), so a ghost row shown on screen
    and the row a 1-9 keypress targets can never be a mismatch."""
    return [(kind, addr) for kind, addr, _ in _open_position_entries()]


_CLOSE_RETRY_ATTEMPTS = 3          # manual 1-9 close retries on transient failures (2026-10-14)
_CLOSE_RETRY_SLEEP_SECS = 3.0      # between attempts; GMGN 429 bans reset ~30-40s, pool-route errors are instant
_CLOSE_RETRYABLE_MARKERS = (
    "blocked", "failed", "rejected", "implausible",
    "rate limit", "little pool", "routeerr",
)
_CLOSE_STOP_MARKERS = ("no open", "not open", "no position")


def _retryable_close_failure(msg: str) -> bool:
    """True if a close result string is a transient failure worth retrying
    (GMGN rate-limit/ban, illiquid-pool route error, implausible quote) --
    and NOT a definitive condition (position genuinely gone). Every closed
    failure path restores the position before returning, so retrying is
    always safe: it either eventually closes, or comes back to the exact
    same untouched state."""
    low = msg.lower()
    if any(m in low for m in _CLOSE_STOP_MARKERS):
        return False
    return any(m in low for m in _CLOSE_RETRYABLE_MARKERS)


def _close_one_attempt(kind: str, address: str) -> str:
    """One single close attempt for one position. Same shape as the old
    _close_one() -- kept as its own function so _close_one() can retry
    transient failures around it. Everything here restores the position
    on failure, never fabricates a result."""
    if kind == "paper":
        try:
            closed = paper_trader.simulate_sell(address, reason="manual")
            return f"PAPER SELL {closed.get('symbol')} pnl={closed.get('pnl_sol'):.5f} SOL"
        except paper_trader.ImplausiblePrice as exc:
            # 2026-09-12, real gap found live: manual sells fetch their own
            # price with no pre-check the way exit_manager.py's automatic
            # loop now has -- this is that same guard, just triggered from
            # here instead. Position is untouched (simulate_sell() restores
            # it before raising) -- safe to just try again in a moment.
            return f"Paper close REJECTED for {address[:8]} -- implausible price quote, position untouched, try again: {exc}"
        except (paper_trader.PriceUnavailable, paper_trader.PositionError) as exc:
            return f"Paper close failed for {address[:8]}: {exc}"
    else:
        try:
            closed = executor.execute_sell(address, reason="manual")
            return f"LIVE SELL submitted for {closed.get('symbol')} — real funds."
        except executor.ExecutionBlocked as exc:
            return f"Live close blocked for {address[:8]}: {exc}"
        except gmgn.GmgnCliError as exc:
            # Real bug found live 2026-09-09: an uncaught GmgnCliError here
            # (e.g. GMGN's own swap router returning "GetSwapRouteErr:
            # little pool hit" for an illiquid pool) propagated all the way
            # out of _handle_key(), crashing the ENTIRE dashboard process --
            # not just failing this one sell. A thin/broken pool is a real,
            # fairly common condition, not something worth taking the whole
            # dashboard down over. Position stays open, nothing was sold
            # on-chain (the failure is at the route-quote stage, before any
            # swap is submitted) -- safe to retry later or leave as a known
            # stuck/illiquid position.
            return f"Live sell FAILED for {address[:8]} (still open, nothing sold): {exc}"


def _close_one(kind: str, address: str) -> str:
    """Close one position, bounded-retrying transient failures. Found live
    2026-10-14: pressing 1-9 while GMGN had the IP rate-limited/banned
    failed on the FIRST attempt and the position stayed open with almost
    no visible feedback -- the operator had to sell the position by hand
    on GMGN. Now: one attempt, then up to N-1 retries with a short pause
    only when the result is a transient failure (rate-limit ban, illiquid-
    pool route error, implausible quote); definitive results ("no open
    position") return immediately; a success returns immediately.
    Worst case the button costs a few extra seconds and reports the
    position is still open -- it never fabricates a close. The brief
    sleep blocks the refresh loop the same way the 'n' note prompt does --
    acceptable for a rare manual action."""
    last = _close_one_attempt(kind, address)
    for attempt in range(1, _CLOSE_RETRY_ATTEMPTS):
        if not _retryable_close_failure(last):
            return last
        time.sleep(_CLOSE_RETRY_SLEEP_SECS * attempt)
        last = _close_one_attempt(kind, address)
    if "rate limit" in last.lower() or "429" in last.lower() or "banned" in last.lower():
        last += " GMGN is rate-limiting you right now -- sell on GMGN directly if urgent, or retry in ~30-40s."
    return last


def _close_position_by_index(n: int) -> str:
    rows = _open_position_rows()
    if n < 1 or n > len(rows):
        return f"No position #{n} open."
    kind, address = rows[n - 1]
    return _close_one(kind, address)


def _sell_all_positions() -> str:
    """Panic-sell -- closes EVERY open position (paper and live both),
    one at a time, immediately. Added 2026-09-06 at the user's explicit
    request for a real one-key "stop everything" button before a live
    test. Deliberately keeps going even if one close fails (a stuck
    position must never block selling the rest) -- reports how many
    actually succeeded vs failed rather than raising."""
    rows = _open_position_rows()
    if not rows:
        return "No open positions to sell."
    ok, failed = 0, 0
    for kind, address in rows:
        result = _close_one(kind, address)
        if "failed" in result.lower() or "blocked" in result.lower():
            failed += 1
        else:
            ok += 1
    return f"SELL ALL: {ok} closed, {failed} failed (of {len(rows)} open)."


def _prompt_user_note() -> str:
    """Modal text-input for the 'n' key -- collects a free-text message
    character by character via msvcrt.getch() until Enter, then calls
    ai_review.write_user_note() directly (2026-10, user's explicit request
    for a dashboard-native way to message the AI instead of only going
    through Claude). Deliberately blocks the refresh loop while typing --
    same tradeoff this module's now-removed _prompt() helper used to make
    -- acceptable since this is a rare, user-initiated action, not
    something firing on a timer like the rest of the loop. Esc cancels
    without writing anything; write_user_note() already has its own
    cross-process lock, so this is safe even while screener.py's review
    loop is mid-read of the same file."""
    console.print("\n[bold cyan]Message to AI[/bold cyan] (Enter to send, Esc to cancel): ", end="")
    chars: list[str] = []
    while True:
        ch = msvcrt.getch()
        if ch in (b"\r", b"\n"):
            console.print()
            break
        if ch == b"\x1b":
            console.print()
            return "Note cancelled."
        if ch in (b"\x08", b"\x7f"):
            if chars:
                chars.pop()
                console.print("\b \b", end="")
            continue
        try:
            decoded = ch.decode()
        except UnicodeDecodeError:
            continue
        if decoded.isprintable():
            chars.append(decoded)
            console.print(decoded, end="")

    text = "".join(chars).strip()
    if not text:
        return "Empty note -- nothing sent."
    try:
        ai_review.write_user_note(text)
    except Exception as exc:  # noqa: BLE001 -- a failed note must never crash the dashboard
        log.exception("tui.py: failed to write user note.")
        return f"Failed to send note (see log): {exc}"
    preview = text if len(text) <= 60 else text[:60] + "..."
    return f"Note sent to AI: {preview}"


def _handle_key(ch: bytes) -> str:
    key = ch.decode(errors="ignore").lower()

    if key == "q":
        raise SystemExit

    if key == "s":
        return _sell_all_positions()

    if key == "n":
        return _prompt_user_note()

    # Instant digit-key close (2026-09-07, replaces the old "x" + type-a-
    # number-then-Enter prompt) -- ch.decode() on a digit byte gives "1".."9"
    # directly, so this fires the same tick as the keypress, no _prompt()
    # detour through console.input().
    if key.isdigit() and key != "0":
        return _close_position_by_index(int(key))

    if key == "v":
        # Voice toggle (2026-10-15). Session-level: the mic stream
        # opens/closes underneath via voice_io per utterance; `v` only
        # flips whether the dashboard keeps re-arming it. When voice is
        # off, `_voice` is None and this honestly says so rather than
        # silently doing nothing -- same reason `l` now explains itself.
        global _voice_session, _voice_pending
        if _voice is None:
            return "Voice is off -- set VOICE_ENABLED=true in .env and restart the dashboard."
        if _voice_session:
            _voice_session = False
            _voice_pending = None
            return _voice.disable_listening()
        msg, ok = _voice.arm_voice()
        _voice_session = ok
        if ok and config.voice_speak_feedback:
            _voice.speak("Voice armed.")   # worker's echo gate holds the mic
        return msg                          # until this finishes playing

    if key in ("o", "k"):
        # Both keys do the same thing -- "k" for the same muscle-memory
        # reason Axiom-bot's own kill switch uses "k" (2026-09-07, user's
        # explicit request for a real shutdown switch).
        trading_control.set_mode("off")
        return "SHUTDOWN -- Trading mode -> OFF. New buys stopped; open positions still exit normally."

    if key == "r":
        trading_control.set_mode("paper")
        return "Trading mode -> PAPER."

    if key == "l":
        # DEACTIVATED 2026-09-16 (user's explicit request: "deactivate that
        # L key from the dashboard so no more accidents happen please") --
        # real incident the same night: a stray "l" keypress silently
        # flipped mode to live with the zero-confirmation design this key
        # used to have, and a real signal fired and bought a real position
        # (POG, 0.06 SOL) before anyone noticed. That position was later
        # sold manually and everything reconciled, but the switch itself
        # was the real, avoidable cause. No longer changes mode at all --
        # just tells you it's off and where the real switch actually lives.
        # To genuinely go live, set TRADING_MODE=live in .env and restart
        # the bot -- a deliberate, harder-to-accidentally-trigger action
        # than a single keypress on a window that might have focus.
        return "Live mode via keypress is disabled -- set TRADING_MODE=live in .env and restart the bot instead."

    return ""


# --- voice commands ------------------------------------------------------------
#
# Maps voice_io's intents onto the exact same action functions the
# keyboard uses -- ZERO new trading logic here, deliberately: voice is a
# second input channel onto the existing keyset, never a third engine.
# Money-touching intents (sell_all/close_one/shutdown/paper) require a
# spoken "confirm" within config.voice_confirm_window_secs first. The
# keyboard does these same actions zero-confirm -- a key is physically
# unambiguous, a transcription is not (see the `l`-incident comment in
# _handle_key above).

_VOICE_ACTION_INTENTS = ("sell_all", "close_one", "shutdown", "paper")

_VOICE_HELP_TEXT = (
    "Voice commands: 'sell all', 'close position one to nine', 'shutdown', 'go paper', "
    "'status', 'balance', 'note to the gentleman ...', 'help', 'voice off'. "
    "Money actions ask for a spoken 'confirm'. Live mode cannot be armed by voice at all."
)


def _voice_say(msg: str) -> None:
    if _voice is not None and config.voice_speak_feedback:
        _voice.speak(msg)


def _describe_pending(intent: str, payload: dict) -> str:
    if intent == "sell_all":
        return f"SELL ALL {len(_open_position_rows())} open position(s)"
    if intent == "close_one":
        return f"close position #{payload.get('index')}"
    if intent == "shutdown":
        return "SHUTDOWN -- trading mode -> OFF"
    if intent == "paper":
        return "trading mode -> PAPER"
    return intent


def _voice_execute(intent: str, payload: dict) -> str:
    """Runs the action itself -- shared by the confirm path. Return
    strings match the keyboard handlers exactly so downstream code can't
    tell which input channel fired."""
    if intent == "sell_all":
        return _sell_all_positions()
    if intent == "close_one":
        return _close_position_by_index(int(payload.get("index", 0)))
    if intent == "shutdown":
        trading_control.set_mode("off")
        return "SHUTDOWN -- Trading mode -> OFF. New buys stopped; open positions still exit normally."
    if intent == "paper":
        trading_control.set_mode("paper")
        return "Trading mode -> PAPER."
    return ""


def _handle_voice_command(cmd) -> str:
    global _voice_pending
    intent = cmd.intent

    if intent is None:
        # Unmatched audio: never acted on, and NOT echoed to the status
        # line either (a status line full of "didn't understand: <room
        # noise>" buries the real messages). voice_io logged it.
        return ""

    if intent == "cancel":
        if _voice_pending:
            _voice_pending = None
            return "Voice action cancelled."
        return ""

    pending = _voice_pending
    if pending:
        intent_name, payload_payload, expires = pending
        if time.time() > expires:
            _voice_pending = None
            _voice_say("Confirmation window expired. Action not taken.")
            return f"Voice {_describe_pending(intent_name, payload_payload)} NOT confirmed in time -- cancelled."
        if intent == "confirm":
            _voice_pending = None
            return _voice_execute(intent_name, payload_payload)
        # Any other utterance while a confirm is pending: cancel rather
        # than chain. A half-heard follow-up sentence must never inherit
        # a yes that wasn't heard clearly. The current utterance is
        # dropped too (not re-dispatched this pass) -- safest reading of
        # "did you mean to answer the confirm?".
        _voice_pending = None
        return f"Voice action cancelled: {_describe_pending(intent_name, payload_payload)} not confirmed."

    if intent == "confirm":
        return "Nothing to confirm."

    if intent == "voice_off":
        global _voice_session
        _voice_session = False
        return _voice.disable_listening()

    if intent == "help":
        _voice_say(_VOICE_HELP_TEXT)
        return _VOICE_HELP_TEXT

    if intent == "status":
        rows = _combined_open_positions()
        unrealized = _unrealized_pnl_sol(rows)
        mode = trading_control.get_mode().upper()
        realized = 0.0
        try:
            realized = sum(t.get("pnl_sol", 0.0)
                           for t in trade_history.load_trades() if t.get("pnl_sol") is not None)
        except Exception:  # noqa: BLE001 -- status readout must never raise
            log.exception("tui.py: voice status realized-PnL read failed")
        msg = (f"{mode} mode. {len(rows)} open. Unrealized {unrealized:+.3f} SOL. "
               f"Realized {realized:+.3f} SOL.")
        _voice_say(f"{mode} mode. {len(rows)} open positions. "
                   f"Unrealized {unrealized:+.2f} sol, realized {realized:+.2f} sol.")
        return msg

    if intent == "note":
        text = str(cmd.payload.get("text") or "").strip()
        if not text:
            return "Note was empty -- nothing sent."
        try:
            ai_review.write_user_note(text)
        except Exception as exc:  # noqa: BLE001 -- same reason as _prompt_user_note's guard
            log.exception("tui.py: voice note failed.")
            return f"Failed to send note (see log): {exc}"
        preview = text if len(text) <= 60 else text[:60] + "..."
        _voice_say(f"Note sent: {preview}")
        return f"Voice note sent to AI: {preview}"

    if intent == "live_refused":
        # Exactly the `l`-key policy (2026-09-16 incident, see _handle_key):
        # live mode needs .env + a restart. A voice mis-hear flipping
        # paper -> live is a strictly worse version of a stray keypress.
        return ("Going live is disabled from the dashboard entirely -- "
                "set TRADING_MODE=live in .env and restart the bot instead.")

    if intent in _VOICE_ACTION_INTENTS:
        _voice_pending = (intent, dict(cmd.payload), time.time() + config.voice_confirm_window_secs)
        ask = f"{_describe_pending(intent, cmd.payload)} -- say confirm, or cancel."
        _voice_say(ask)
        return ask

    return ""


def main() -> None:
    global _voice, _voice_session
    status_msg = ""
    status_expiry = 0.0
    if config.voice_enabled and VoiceIO is not None:
        # start() returns immediately -- model download/load happens on the
        # voice threads, surfaced via the header badge, never on this path.
        _voice = VoiceIO()
        _voice.start()
    elif config.voice_enabled:
        log.warning("tui.py: VOICE_ENABLED=true but voice_io failed to import -- running voiceless.")
    with Live(render(), console=console, screen=True, refresh_per_second=10) as live:
        last_refresh = 0.0
        try:
            while True:
                now = time.time()
                if status_msg and now > status_expiry:
                    status_msg = ""
                if now - last_refresh >= REFRESH_SECS:
                    live.update(render(status_msg))
                    last_refresh = now
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    try:
                        result = _handle_key(ch)
                    except SystemExit:
                        break
                    except Exception as exc:  # noqa: BLE001 -- found live 2026-09-10:
                        # two prior crashes here (both an uncaught GmgnCliError
                        # from a specific live-sell edge case) each got a narrow,
                        # one-off fix inside _close_one() -- but the main loop
                        # itself never got a catch-all, unlike every other loop
                        # in this codebase (screener.py/exit_manager.py/
                        # ai_review.py all wrap their own loops in `except
                        # Exception` specifically so one bad operation can't take
                        # the whole process down). A THIRD, different uncaught
                        # exception from any keypress -- not just the two already
                        # special-cased -- would still crash this whole dashboard.
                        # This module's own docstring claims "a bug or crash here
                        # can't affect trading" -- true for the trading engine
                        # (screener.py runs independently), but false for the
                        # dashboard itself staying up, which matters just as much
                        # given how central this is to actually watching him.
                        log.exception("tui.py: unhandled exception from key %r -- dashboard stays up.", ch)
                        result = f"Internal error handling that key (see log): {exc}"
                    if result:
                        status_msg = result
                        status_expiry = time.time() + STATUS_MSG_TTL_SECS
                    live.update(render(status_msg))
                # Voice (2026-10-15). Keyboard above this tick wins outright:
                # a key the operator actually pressed beats anything the mic
                # may still be thinking about. Session model: tui owns the
                # arm/disarm decision, voice_io reopens capture per utterance
                # while armed (keep_armed holds off until our own TTS is
                # silent -- the echo gate has two layers now, this one stops
                # RE-ARMING, voice_io's own gate stops CAPTURING).
                if _voice is not None:
                    if _voice_session:
                        _voice.keep_armed()
                    for cmd in _voice.drain_commands():
                        try:
                            result = _handle_voice_command(cmd)
                        except Exception as exc:  # noqa: BLE001 -- identical
                            # policy to the keypress catch-all above: a bad
                            # transcript must never take the dashboard down.
                            log.exception("tui.py: unhandled exception from voice command %r -- dashboard stays up.", cmd.text)
                            result = f"Internal error handling that command (see log): {exc}"
                        if result:
                            status_msg = result
                            status_expiry = time.time() + STATUS_MSG_TTL_SECS
                            live.update(render(status_msg))
                time.sleep(0.02)
        except KeyboardInterrupt:
            pass
    if _voice is not None:
        _voice.stop()
    console.print("[yellow]Terminal control dashboard stopped — bot/screener.py keeps running independently.[/yellow]")


if __name__ == "__main__":
    main()
