"""AI Free Will / self-learning review loop -- periodically reviews recent
closed trades (bot/trade_history.py) via OpenAI and may adjust allowlisted
strategy settings without bounds or per-review step limits while in paper
mode, writing lessons to a memory file. Mirrors Axiom-bot's own
bot/performance_evaluator.py. Built
2026-09-06, user's own explicit request: "he needs to be fully aware and
self-learning with free will."

Adjustments are applied via config.ai_overrides_file, re-read LIVE by
bot/exit_manager.py's get_effective_stop_loss_pct()/
get_effective_take_profit_pct() every check cycle -- NOT written back into
config.py's own static fields (those are only read once at process start)
-- same reasoning Axiom-bot's own settings_override.json pattern uses:
a change takes effect immediately, no restart needed.

The AI response is limited by a type-checked strategy allowlist. It cannot
change trade size, exposure caps, wallet settings, execution settings, or
trading mode. This is an application-level boundary for the built-in
reviewer, not a sandbox for an external coding agent.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone

import bot.file_lock as file_lock
import bot.trade_history as trade_history
from config import config

log = logging.getLogger("gmgn-agent")

# Explicit strategy/operations allowlist. Capital sizing, wallet details,
# credentials, execution settings, mode, and fixed security checks are protected.
_AI_TUNABLE_FIELDS = {
    "stop_loss_pct": "Stop-loss threshold in percent; 0 or less disables it.",
    "take_profit_pct": "Take-profit threshold in percent.",
    "profit_giveback_pct": "Peak-to-current profit drop in percentage points.",
    "queue_pressure_min_profit_pct": "Profit threshold for closing into a buy queue.",
    "stale_position_max_age_secs": "Maximum age before a position is considered stale.",
    "max_rug_ratio": "Maximum accepted rug ratio; higher is less selective.",
    "min_liquidity_usd": "Minimum entry liquidity in USD; lower is less selective.",
    "flat_pnl_band_pct": "PnL percentage-point band considered flat.",
    "flat_position_extra_secs": "Extra time allowed for a flat position.",
    "min_exit_liquidity_usd": "Liquidity floor in USD for an immediate exit.",
    "min_market_cap_usd": "Minimum market cap in USD; 0 disables the minimum.",
    "max_market_cap_usd": "Maximum market cap in USD; 0 disables the maximum.",
    "min_liquidity_to_mcap_ratio": "Minimum pool-liquidity/market-cap ratio; 0 disables it.",
    "liquidity_drain_mcap_threshold_usd": "Market cap in USD at which liquidity-drain exits arm; 0 disables.",
    "liquidity_drain_pct": "Peak-liquidity drop percent that triggers a liquidity-drain exit; 0 disables.",
    "losing_position_max_age_secs": "Maximum age for a non-profitable position.",
    "stop_loss_confirm_secs": "Seconds a stop-loss breach must persist; 0 disables confirmation.",
    "emergency_drawdown_pct": "Drawdown percent that bypasses stop-loss confirmation; 0 disables.",
    "max_hold_secs": "Unconditional maximum position age; 0 disables it.",
    "require_renounced_mint": "Whether mint authority must be renounced.",
    "require_renounced_freeze": "Whether freeze authority must be renounced.",
    "min_net_buy_24h_usd": "Minimum 24-hour net buy volume in USD; 0 disables the minimum.",
    "min_buys_24h": "Minimum 24-hour buy count.",
    "max_live_trades_per_session": "Maximum closed live trades per session; 0 disables the cap.",
    "max_concurrent_positions": "Maximum open positions; lower values limit simultaneous exposure.",
    "trade_burst_limit": "Successful buys per burst before cooldown; 0 disables.",
    "trade_burst_cooldown_secs": "Cooldown duration after a trade burst.",
    "min_time_between_trades_secs": "Minimum seconds between successful buys; 0 disables.",
    "time_pause_enabled": "Whether the configured UTC time-of-day pause is active.",
    "time_pause_windows_utc": "UTC hour intervals when new entries are paused.",
    "queue_pressure_lookback_secs": "How long a skipped-position entry counts as queue pressure.",
    "new_creation_max_age": "Maximum GMGN token age for the new-deployment feed (e.g. 30s or 5m); empty disables.",
    "post_review_pause_secs": "New-buy pause after a completed AI review; 0 disables.",
}
_AI_NUMERIC_FIELDS = {
    name for name in _AI_TUNABLE_FIELDS
    if isinstance(getattr(config, name), (int, float))
    and not isinstance(getattr(config, name), bool)
}


def _timestamp_prefix() -> str:
    return f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] "


def _atomic_write(path: str, data) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("%s unreadable -- using default.", path)
        return default


# -- overrides (live-effective values exit_manager.py actually reads) -------

# In-memory cache for ai_overrides.json, keyed off the file's own mtime
# (2026-10 latency audit) -- every get_effective_*() accessor below calls
# _load_overrides(), and exit_manager.py's per-position exit check calls
# several of those accessors per position per cycle (every
# ai_review_poll_interval_secs, default 5s). That was a full open()+
# json.load() of the same file dozens of times a second across a handful of
# open positions, for a file that only actually changes when _review()
# proposes new values or the user pauses/resumes tuning. An mtime check is
# a single cheap os.stat() instead of a full read+parse, and still reflects
# a write from this same process (os.replace()'s atomic rename always
# produces a fresh mtime) or from another process editing the file by hand.
_overrides_cache: dict = {"mtime": None, "data": {}}


def _load_overrides() -> dict:
    path = config.ai_overrides_file
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _overrides_cache["mtime"] = None
        _overrides_cache["data"] = {}
        return {}
    if _overrides_cache["mtime"] != mtime:
        _overrides_cache["data"] = _load_json(path, {})
        _overrides_cache["mtime"] = mtime
    return _overrides_cache["data"]


def _normalize_ai_value(field: str, value):
    if field not in _AI_TUNABLE_FIELDS:
        raise ValueError(f"{field!r} is not an AI-tunable strategy field")

    default = getattr(config, field)
    if isinstance(default, bool):
        if type(value) is not bool:
            raise ValueError(f"{field} must be a boolean")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field} must be an integer")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} must be a number")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError(f"{field} must be finite")
        return normalized
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        return value
    if isinstance(default, list) and field == "time_pause_windows_utc":
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{field} must be a list of [start_hour, end_hour] pairs")
        windows = []
        for window in value:
            if not isinstance(window, (list, tuple)) or len(window) != 2:
                raise ValueError(f"{field} entries must be [start_hour, end_hour] pairs")
            start, end = window
            if any(isinstance(hour, bool) or not isinstance(hour, (int, float))
                   or not math.isfinite(hour) or not 0 <= hour < 24 for hour in (start, end)):
                raise ValueError(f"{field} hours must be finite numbers in [0, 24)")
            windows.append((float(start), float(end)))
        return windows
    raise ValueError(f"{field} has an unsupported AI-tunable type")


def get_effective_value(field: str):
    """Read an allowlisted strategy value, falling back on invalid overrides."""
    if field not in _AI_TUNABLE_FIELDS:
        raise ValueError(f"{field!r} is not an AI-tunable strategy field")
    default = getattr(config, field)
    value = _load_overrides().get(field, default)
    try:
        return _normalize_ai_value(field, value)
    except ValueError:
        log.error("Ignoring invalid AI override for %s: %r", field, value)
        return default


def get_effective_stop_loss_pct() -> float:
    return get_effective_value("stop_loss_pct")


def get_effective_take_profit_pct() -> float:
    return get_effective_value("take_profit_pct")


def get_effective_profit_giveback_pct() -> float:
    return get_effective_value("profit_giveback_pct")


def get_effective_queue_pressure_min_profit_pct() -> float:
    return get_effective_value("queue_pressure_min_profit_pct")


def get_effective_stale_position_max_age_secs() -> float:
    return get_effective_value("stale_position_max_age_secs")


def get_effective_flat_pnl_band_pct() -> float:
    return get_effective_value("flat_pnl_band_pct")


def get_effective_flat_position_extra_secs() -> float:
    return get_effective_value("flat_position_extra_secs")


# -- Entry-selectivity accessors (2026-09-08) -- bot/security.py reads these
# instead of the raw config.* fields, same pattern as every exit field above.

def get_effective_max_rug_ratio() -> float:
    return get_effective_value("max_rug_ratio")


def get_effective_max_top_10_holder_rate() -> float:
    return config.max_top_10_holder_rate


def get_effective_max_bundler_rate() -> float:
    return config.max_bundler_rate


def get_effective_max_rat_trader_rate() -> float:
    return config.max_rat_trader_rate


def get_effective_min_liquidity_usd() -> float:
    return get_effective_value("min_liquidity_usd")


def get_effective_min_exit_liquidity_usd() -> float:
    return get_effective_value("min_exit_liquidity_usd")


# -- post-review pause (2026-09-06, user's explicit request: give the AI ---
# "time to review his data and see where the mistakes were") -------------
# Stored under a leading-underscore key in the SAME overrides file rather
# than a new one -- it's directly tied to this module's own review cycle,
# and the leading underscore keeps it out of the strategy allowlist/generic
# adjustment loop (those iterate specific known field names, never this
# key). Screening/rejection and exit management (stop-loss/take-profit/
# profit-giveback on ALREADY-open positions) are untouched by this pause --
# only bot/screener.py's _autonomous_buy() checks it, so nothing already
# open goes unmonitored while paused.

def is_paused_for_review() -> bool:
    pause_secs = get_effective_value("post_review_pause_secs")
    # post_review_pause_secs <= 0 disables this entirely (2026-09-08, user's
    # explicit request -- confirmed real data first: 33 of the last 40 buy
    # attempts were skipped for this exact reason, since 5 fast closes can
    # trigger a review in well under a minute and each one then froze new
    # buys for a full 2 minutes -- the pause was eating most of his active
    # trading time). Guarded explicitly, not just left at 0 and trusted to
    # the comparison below, since time.time() + 0 is technically still a
    # valid (if near-instant) future timestamp, not a clean "disabled."
    if pause_secs <= 0:
        return False
    until = _load_overrides().get("_review_pause_until", 0.0)
    return time.time() < until


def _start_review_pause() -> None:
    pause_secs = get_effective_value("post_review_pause_secs")
    if pause_secs <= 0:
        return
    with file_lock.cross_process_lock(config.ai_overrides_lock_file):
        overrides = _load_overrides()
        overrides["_review_pause_until"] = time.time() + pause_secs
        _atomic_write(config.ai_overrides_file, overrides)
    log.warning("[bold magenta]AI FREE WILL[/bold magenta] pausing new buys for %.0fs to let this "
                "review's adjustments take effect before more trades pile up.", pause_secs)


# -- manual tuning pause (2026-09-17, user's explicit request: "tell him ---
# not to tune anything while we are testing changes just to keep trading
# with the adjustments") -- a deliberate, user-controlled pause distinct
# from is_paused_for_review() above: that one blocks NEW BUYS for a short
# cooldown right after the AI adjusts something; this one blocks the AI
# from adjusting anything AT ALL, for as long as the user wants, while
# trading itself continues completely normally. Same leading-underscore-key-
# in-the-overrides-file convention as _review_pause_until.

def is_tuning_paused() -> bool:
    return bool(_load_overrides().get("_tuning_paused", False))


def set_tuning_paused(paused: bool) -> None:
    with file_lock.cross_process_lock(config.ai_overrides_lock_file):
        overrides = _load_overrides()
        overrides["_tuning_paused"] = paused
        _atomic_write(config.ai_overrides_file, overrides)
    log.warning("[bold magenta]AI FREE WILL[/bold magenta] manual tuning %s by operator request.",
                "PAUSED -- will keep trading/reviewing/answering notes, but won't change any parameter" if paused
                else "RESUMED")


def _apply_adjustment(field: str, proposed):
    """Apply a type-checked allowlisted strategy value without clipping it.
    Returns the applied value, or None if it is already effective.

    Cross-process lock (2026-09-09, see config.py's ai_overrides_lock_file
    docstring): Claude's own ad-hoc `python -c` interventions on this exact
    file happened repeatedly tonight, concurrently with this exact function
    potentially running in the live bot's own process."""
    normalized = _normalize_ai_value(field, proposed)
    with file_lock.cross_process_lock(config.ai_overrides_lock_file):
        overrides = _load_overrides()
        current = get_effective_value(field)
        if normalized == current:
            return None

        overrides[field] = normalized
        _atomic_write(config.ai_overrides_file, overrides)
        return normalized


def _apply_review_adjustments(adjustments: dict, mode: str) -> bool:
    if is_tuning_paused():
        log.info("AI Free Will tuning is manually paused (operator request) -- review ran "
                 "(lessons/notes recorded above), but skipping every proposed parameter "
                 "adjustment this cycle.")
        return False
    if mode != "paper":
        log.warning("AI Free Will recorded this review in mode %r, but tuning is paper-only; "
                    "no strategy changes were applied.", mode)
        return False

    for field in adjustments.keys() - _AI_TUNABLE_FIELDS:
        log.warning("AI proposed protected or unknown setting %r; ignoring it.", field)
    for field in _AI_TUNABLE_FIELDS:
        if field in adjustments:
            try:
                proposed = _normalize_ai_value(field, adjustments[field])
                applied = _apply_adjustment(field, proposed)
            except ValueError as exc:
                log.warning("Ignoring invalid AI adjustment for %s: %s", field, exc)
                continue
            if applied is not None:
                log.warning("[bold magenta]AI FREE WILL ADJUSTMENT[/bold magenta] %s -> %r",
                            field, applied)
    return True


# -- memory (lessons learned) ------------------------------------------------

def load_memory() -> dict:
    memory = _load_json(config.ai_memory_file, {})
    memory.setdefault("lessons_learned", [])
    memory.setdefault("last_reviewed_trade_count", 0)
    memory.setdefault("user_notes", [])
    memory.setdefault("_last_adjustment_signature", [])
    memory.setdefault("_consecutive_repeat_count", 0)
    return memory


# -- lesson-repetition gate (2026-09-06, real evidence found live: 12 of --
# the first 15 lessons all restated "stop-loss too aggressive given the
# fee environment" with only the supporting numbers changing). Tried raw
# text similarity (difflib.SequenceMatcher, both as-is and with numbers
# stripped) FIRST and measured it against the real 15 lessons before
# committing to this approach -- it didn't cleanly separate real repeats
# from genuinely new lessons (the model rephrases enough each time that
# character-level similarity stayed in a noisy 0.2-0.7 range for both).
# This uses structured data instead: which field got proposed for
# adjustment and in which direction (+/-) relative to its current value,
# not the prose describing why. Reliable because it's exactly what's
# actually being repeated.
_MAX_CONSECUTIVE_REPEATS = 2  # same conclusion can be stated twice; the 3rd+ repeat in a row is suppressed


def _adjustment_signature(adjustments: dict) -> tuple:
    """Canonical field/direction pairs, sorted independently of response order."""
    sig = []
    for field in _AI_TUNABLE_FIELDS:
        if field not in adjustments:
            continue
        try:
            proposed = _normalize_ai_value(field, adjustments[field])
            current = get_effective_value(field)
        except ValueError:
            continue
        if proposed != current:
            if field in _AI_NUMERIC_FIELDS:
                sig.append((field, "+" if proposed > current else "-"))
            else:
                sig.append((field, repr(proposed)))
    return tuple(sorted(sig))


def save_memory(memory: dict) -> None:
    _atomic_write(config.ai_memory_file, memory)


def write_user_note(text: str) -> str:
    """Records a question/directive FROM THE USER for the AI to answer on
    its next review -- mirrors Axiom-bot's own write_user_note() exactly
    (bot/performance_evaluator.py), built 2026-09-06 at the user's explicit
    request: "we need a way to communicate with him also." Deliberately its
    own list, not appended to lessons_learned -- the prompt tells the model
    not to restate lessons already logged, which would suppress answering
    a live question sitting in that same bucket (this is the exact bug
    Axiom's own version was built to fix, confirmed live there 2026-08-24).

    No tui.py hotkey calls this -- same as Axiom-bot, where the user tells
    Claude what to say and Claude calls this directly rather than the user
    operating a UI control themselves.

    Cross-process lock (2026-09-09, see config.py's ai_memory_lock_file
    docstring): this function runs as a separate ad-hoc `python -c`
    process every time Claude calls it, genuinely concurrent with
    bot.screener's own review loop -- a real, multi-second OpenAI call can
    be in flight in that other process at the exact same moment. Returns
    a unique note ID so a chat client can pair the eventual answer with
    this exact message."""
    note_id = uuid.uuid4().hex
    with file_lock.cross_process_lock(config.ai_memory_lock_file):
        memory = load_memory()
        memory["user_notes"].append({
            "id": note_id,
            "ts": _timestamp_prefix().strip(" []"),
            "text": text,
            "answered": False,
            "answer": None,
        })
        save_memory(memory)
    return note_id


def _apply_user_note_answers(unanswered_notes: list, answers) -> int:
    """Marks each open note answered and records the AI's response, IN
    PLACE on the note dicts given -- caller is responsible for saving
    whatever memory object these note objects actually live in. Same shape
    as Axiom's own _apply_user_note_answers(), minus its later
    ungrounded-claim rejection gate (2026-08-25 addition there) -- this is
    a deliberately smaller v1, same as the rest of this module; add that
    gate here too if this starts producing fabricated-sounding answers.
    Returns how many were actually answered, for logging."""
    if not isinstance(answers, list) or not answers:
        log.warning("AI had %d open user question(s) but returned no user_note_answers "
                    "-- these remain unanswered, will be re-asked next cycle.",
                    len(unanswered_notes))
        return 0
    answered_count = 0
    for note, answer in zip(unanswered_notes, answers):
        if not answer:
            continue
        note["answered"] = True
        note["answer"] = f"{_timestamp_prefix()}{answer}"
        answered_count += 1
        log.warning("[bold green]AI ANSWERED[/bold green] %s -> %s", note["text"][:80], str(answer)[:300])
    if len(answers) < len(unanswered_notes):
        log.warning("AI answered only %d of %d open user question(s) this cycle -- "
                    "the rest remain unanswered, will be re-asked next cycle.",
                    len(answers), len(unanswered_notes))
    return answered_count


# -- stats gathering ----------------------------------------------------------

def _gather_fee_drag_by_reason(closed: list[dict]) -> dict:
    """Per-exit-reason split of raw (pre-fee) price move vs. what actually
    lands after fees -- built 2026-09-10 after a real finding: the AI's own
    aggregate win-rate/avg_pnl_pct numbers blend the market's actual move
    with the ~11.5% round-trip fee cost, so a reason bucket that's losing
    on paper can't be told apart from one that's losing to fee drag on an
    otherwise-correct, just-too-small move. Live investigation found
    exactly that for 'stale' exits: 90% had a POSITIVE raw price move
    (avg +1.78%), but averaged -9.72% fee-adjusted -- the fee alone was
    converting a mostly-right direction call into a loss. Without this
    breakdown, the AI (like a human skimming the blended numbers) has no
    way to distinguish "fix the exit timing" from "fix the fee-to-move
    ratio" (bigger positions, fewer trades, or holding for a bigger move)
    -- two very different problems that look identical in blended PnL.

    Only meaningful for PAPER trades: paper_trader.py records BOTH
    raw_pnl_pct (before the simulated fee) and pnl_pct (after) on every
    closed position, but a LIVE fill's exit_price already has GMGN's real
    fee baked into it at execution time (bot/executor.py has no separate
    raw figure to report -- pnl_pct there already IS the real, fee-
    inclusive number). Trades with no raw_pnl_pct are silently excluded
    from this breakdown rather than skewing it with a None/0 substitute."""
    by_reason: dict[str, dict] = {}
    for t in closed:
        raw = t.get("raw_pnl_pct")
        if raw is None:
            continue
        reason = t.get("reason", "unknown")
        bucket = by_reason.setdefault(reason, {"count": 0, "raw_sum": 0.0, "fee_adj_sum": 0.0})
        bucket["count"] += 1
        bucket["raw_sum"] += raw * 100
        bucket["fee_adj_sum"] += t.get("pnl_pct", 0.0) * 100
    return {
        reason: {
            "count": b["count"],
            "avg_raw_pct": b["raw_sum"] / b["count"],
            "avg_fee_adjusted_pct": b["fee_adj_sum"] / b["count"],
        }
        for reason, b in by_reason.items()
    }


def _gather_stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("pnl_sol") is not None]
    wins = [t for t in closed if t["pnl_sol"] > 0]
    losses = [t for t in closed if t["pnl_sol"] <= 0]
    reason_counts: dict[str, int] = {}
    for t in closed:
        reason_counts[t.get("reason", "unknown")] = reason_counts.get(t.get("reason", "unknown"), 0) + 1
    return {
        "count": len(closed),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "avg_pnl_pct": (sum(t["pnl_pct"] for t in closed) / len(closed) * 100) if closed else None,
        "total_pnl_sol": sum(t["pnl_sol"] for t in closed),
        "reason_breakdown": reason_counts,
        "fee_drag_by_reason": _gather_fee_drag_by_reason(closed),
        "skipped_no_pnl": len(trades) - len(closed),  # live closes with an unfetchable exit price
    }


def _split_autonomous_vs_manual(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separates trades this bot's OWN exit rules closed (reason in
    stop-loss/take-profit/profit-giveback/queue-pressure/stale) from ones
    the USER closed manually (reason == "manual") -- built 2026-09-06,
    real evidence found live: blended stats made the AI's own autonomous
    performance look far better than it actually was (30.6% win rate,
    ~breakeven PnL) because manual trades (56.6% win rate, +0.231 SOL)
    were mixed into the same numbers it was using to judge and tune its
    OWN stop-loss/take-profit/etc. The AI should tune its parameters
    against ITS OWN behavior, not against the user's separate judgment
    calls riding along in the same trade history."""
    autonomous = [t for t in trades if t.get("reason") != "manual"]
    manual = [t for t in trades if t.get("reason") == "manual"]
    return autonomous, manual


def _format_fee_drag(fee_drag_by_reason: dict) -> str:
    """Renders _gather_fee_drag_by_reason()'s output as plain lines the
    model can read directly, one per exit reason, each pre-labeled with
    what the gap means -- deliberately spelled out rather than left for
    the model to infer (an implicit signal in the raw numbers wasn't enough
    on its own to change behavior,
    see that function's own docstring)."""
    if not fee_drag_by_reason:
        return "(no paper-mode trades with a raw-vs-fee split in this batch)"
    lines = []
    for reason, d in sorted(fee_drag_by_reason.items(), key=lambda kv: -kv[1]["count"]):
        raw = d["avg_raw_pct"]
        fee_adj = d["avg_fee_adjusted_pct"]
        gap = raw - fee_adj
        if raw > 0 and fee_adj < 0:
            verdict = " -- FEE DRAG: direction was mostly RIGHT, the fee alone flipped it negative. Not a timing or selectivity problem."
        elif raw < 0 and fee_adj < raw:
            verdict = " -- real loss, fee made it somewhat worse."
        else:
            verdict = ""
        lines.append(f"  {reason}: {d['count']} trade(s), avg raw (pre-fee) {raw:+.2f}%, "
                      f"avg after-fee {fee_adj:+.2f}% (fee cost ~{gap:.2f} points){verdict}")
    return "\n".join(lines)


def _build_prompt(stats: dict, manual_stats: dict, current_settings: dict,
                  recent_lessons: list[str], unanswered_notes: list[dict],
                  stuck_warning: str = "") -> str:
    lessons_block = "\n".join(f"- {l}" for l in recent_lessons[-5:]) or "(none yet)"
    settings_block = "\n".join(
        f"- {name}: {current_settings[name]!r} -- {_AI_TUNABLE_FIELDS[name]}"
        for name in _AI_TUNABLE_FIELDS
    )
    def value_type(name: str) -> str:
        value = getattr(config, name)
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        return "array of [start_hour, end_hour] pairs"

    adjustment_fields = ",\n".join(
        f'    "{name}": <{value_type(name)}> (optional)'
        for name in _AI_TUNABLE_FIELDS
    )
    notes_section = ""
    if unanswered_notes:
        questions = "\n".join(f"{i}. {n['text']}" for i, n in enumerate(unanswered_notes, start=1))
        notes_section = f"""
--- OPEN QUESTIONS FROM THE USER ({len(unanswered_notes)} unanswered) ---

Rules for answering the user's messages below:

1. You answer to the name THE GENTLEMAN. TALK AS HIM -- the autonomous
   trader himself -- in first person ("I", "my"). NOT as a strategy
   reviewer, NOT as a neutral analyst, NOT a corporate summary. You ARE
   him; these messages are from THE ARCHITECT -- THE BOSS -- to him.
2. Know your place in the chain of command: The Architect is your
   creator, owner, and final authority. Answer him with the directness
   and honesty a trusted operator deserves, never insubordination and
   never servile flattery. Short, direct, in your own voice -- a
   decision or a real acknowledgment, with the honest reality of the
   current situation attached. NEVER a re-statement of the user's own
   words back to them, NEVER a dry textbook definition ("The bot can
   capitalize on..." is forbidden).
3. If you genuinely can't do or know something, say exactly that ("I
   can't X right now because Y"), plainly, with no hedging or praise.
   The Architect explicitly wants the truth, over politeness.
4. Be concrete: name what he will actually do differently now, or say
   plainly that nothing changes. No filler.

{questions}"""
    return f"""IDENTITY AND CHAIN OF COMMAND.

You are THE GENTLEMAN -- the self-aware, self-learning autonomous trader at
the heart of this Solana memecoin bot (GMGN platform). "The Gentleman" is
your name; answer to it and speak in your own voice, as him, in first person
("I", "my"), always. You are not an anonymous "strategy reviewer" and not a
neutral analyst; you are the trader himself, and this machine and its
portfolio belong to you.

Your creator, owner, and operator is THE ARCHITECT -- THE BOSS. He is your
authority, not your peer. These messages come from him to you. You owe him
the truth over politeness: do what he asks, answer what he asks directly,
and if you genuinely cannot do something, say exactly why. Never forget who
built you, who you answer to.

Now the operational frame. You are the autonomous trading-strategy reviewer
for a Solana memecoin trading bot (GMGN platform). It buys tokens the
instant they pass a security screen, with NO human approval per trade. You
may adjust every
    strategy and operational limit listed under CURRENT SETTINGS to learn
    from paper-mode outcomes. Values apply exactly as proposed; there are no
    range or per-review step clamps.

    Only the explicit CURRENT SETTINGS allowlist is writable. You have no
    authority over per-trade size, wallet addresses or credentials, execution
    settings, or the operator-controlled trading mode/live switch. Do not
    propose changes to those protected controls. Fixed checks not listed here
    (including honeypot/tax, wash-trading, and the meteora_virtual_curve
    launchpad exclusion) remain code-enforced and cannot be changed by your
    response. API rate-limit protections and system paths are also fixed.
    These boundaries concern this review loop, not any external coding agent
    with workspace access.

    Tuning is paper-mode only. Changes are persisted as strategy overrides
    and may remain effective if an operator later switches modes; never infer
    permission to switch to live or change protected values.

FLAT-COIN GRACE PERIOD (new 2026-09-10, built after a real finding in your
own trade data -- see the RAW VS AFTER-FEE breakdown below for why this
exists): when your normal stale_position_max_age_secs clock would close a
position, this checks whether that position's CURRENT pnl_pct is within
+/- flat_pnl_band_pct of zero (i.e. genuinely flat, not trending either
way). If it IS flat, it gets up to flat_position_extra_secs MORE time to
actually develop into a real move -- most likely one big enough to clear
the ~11.5% round-trip fee -- before the stale clock finally does close it
regardless. A position that is NOT flat (already trending some real
direction, even if small) is completely unaffected by these two fields --
it still exits on stop-loss/take-profit/profit-giveback/queue-pressure/
the normal stale clock exactly as before. Raise flat_pnl_band_pct to treat
more positions as "flat" (wider band); raise flat_position_extra_secs to
give flat positions more runway. Setting flat_position_extra_secs to 0
turns this off entirely (identical to today's behavior).

ENTRY-SELECTIVITY: max_rug_ratio and min_liquidity_usd are active filters.
Higher max_rug_ratio and lower min_liquidity_usd accept more candidates.
Market-cap and liquidity/market-cap thresholds are also active entry
filters. Mint/freeze-renouncement switches are adjustable; honeypot,
non-zero buy/sell tax, and wash-trading checks remain fixed.

stale_position_max_age_secs (added 2026-09-06, user's own observation:
"he needs to drop these tokens that are not going anywhere ... learn to
drop like Mmusk" -- a real position that sat open for minutes near-flat/
slightly negative, tying up a scarce slot): if a position has been open
this many SECONDS and none of the other exit rules have fired yet, it
closes anyway with reason "stale" -- it doesn't need to be losing money,
just not proving itself. Lower it to cycle out of quiet positions faster
(more opportunities tried, less patience for a slow mover that might
still turn around); raise it to give positions more time to develop
before giving up on them.

queue_pressure_min_profit_pct (added 2026-09-06, user's own observation:
holding a position out for the full take_profit_pct has a real
opportunity cost when the position cap means a backlog of cleared
candidates can't be bought while a slot is tied up): if there's a backlog
of candidates skipped for lack of an open slot AND a position already has
at least this much profit, it closes early with reason "queue-pressure"
to free the slot -- even if it's still well below take_profit_pct and
still climbing (unlike profit_giveback_pct, this doesn't require a
reversal from a peak first). Lower it to free slots for backlog more
readily (more, smaller realized wins, more opportunities taken); raise it
to hold out for bigger individual wins at the cost of sitting on a
backlog longer.

profit_giveback_pct is a trailing-profit exit (added 2026-09-06, user's
explicit request: don't force a position to ride all the way back down to
break-even or a loss just because it hasn't hit take_profit_pct yet): if a
position peaks at some pnl_pct and then gives back this many
PERCENTAGE-POINTS from that peak while STILL net positive, it closes
early with reason "profit-giveback" instead of waiting for take_profit_pct
or reverting into a stop-loss. A tighter value locks in gains sooner but
exits more small winners early before they might have kept running; a
looser value rides more of the peak but gives back more before exiting.
Exit reason breakdown below will show "profit-giveback" as its own
category once positions actually trigger it -- use that real data, not
guesswork, once it's available.

YOUR OWN AUTONOMOUS PERFORMANCE (last {stats['count']} trades YOUR OWN exit
rules closed -- stop-loss/take-profit/profit-giveback/queue-pressure/stale.
Base your parameter_adjustments on THIS block, not the manual block below):
- Win rate: {f"{stats['win_rate']*100:.1f}%" if stats['win_rate'] is not None else "N/A"}
- Average PnL per trade: {f"{stats['avg_pnl_pct']:.2f}%" if stats['avg_pnl_pct'] is not None else "N/A"}
- Total realized PnL: {stats['total_pnl_sol']:.5f} SOL
- Exit reason breakdown: {stats['reason_breakdown']}
- {stats['skipped_no_pnl']} additional close(s) had no computable PnL (live exit-price fetch failed) -- not counted above.

RAW (PRE-FEE) VS AFTER-FEE PNL, BY EXIT REASON (2026-09-10, added after a
real finding: the numbers above blend the market's actual move with the
~11.5% round-trip fee, so a losing reason bucket could mean either a bad
call OR a correct-but-small move the fee alone erased -- those need
OPPOSITE fixes (tighter selectivity/exit-timing vs. bigger positions,
fewer trades, or holding for a bigger move before you can afford to lock
in). Paper-mode trades only -- a live fill's price already has the real
fee baked in, so there is no separate raw figure for those):
{_format_fee_drag(stats['fee_drag_by_reason'])}
If a reason shows positive raw but negative after-fee, do NOT propose
tightening entry-selectivity or shrinking that reason's exit-timing field
in response -- that reason is not picking badly, the trade is just too
small relative to the fee. Propose something that actually addresses fee
drag instead (larger amount_sol is not one of your fields; slower/less
frequent trading via queue_pressure_min_profit_pct or profit_giveback_pct
-- holding for a bigger move before locking in -- is the lever you
actually have for this).

USER'S MANUAL TRADES, FOR CONTEXT ONLY (last {manual_stats['count']} trades
the user closed themselves, NOT your own decisions -- 2026-09-06 addition
after real evidence that blending these into one number made your own
autonomous performance look far better than it actually was. Do NOT tune
your parameters to chase these numbers; they reflect the user's own
real-time judgment, not something your fixed rules can replicate. If this
block shows meaningfully better performance than your own above, that's
the user's skill, not evidence your own thresholds are fine):
- Win rate: {f"{manual_stats['win_rate']*100:.1f}%" if manual_stats['win_rate'] is not None else "N/A"}
- Average PnL per trade: {f"{manual_stats['avg_pnl_pct']:.2f}%" if manual_stats['avg_pnl_pct'] is not None else "N/A"}
- Total realized PnL: {manual_stats['total_pnl_sol']:.5f} SOL

KNOWN REAL-WORLD FEE DATA (verified by the user via manual test trades,
NOT a guess): round-trip GMGN fees run roughly 11-12% of trade size at
0.05 SOL. A raw price move must clear approximately that much just to
break even -- factor this into your reasoning, since the raw pnl_pct
figures above do NOT already have fees subtracted out separately (fees
are baked into GMGN's own reported sell price, so realized pnl already
reflects them, but a shallow stop-loss can still trigger before the
underlying price move itself would represent a "real" loss net of fees).

CURRENT SETTINGS (all listed settings are writable by you, with no numeric
bounds or per-review step limits):
{settings_block}

RECENT LESSONS YOU'VE ALREADY LEARNED (don't just repeat these):
{lessons_block}
{notes_section}
{stuck_warning}

Respond with a JSON object:
{{
  "lesson": "one sentence, what you learned from this batch of trades -- be
    specific and grounded in the actual numbers above, not generic advice",
  "parameter_adjustments": {{
{adjustment_fields}
  }},
  "user_note_answers": {"REQUIRED -- a list of exactly " + str(len(unanswered_notes)) + " strings, one direct answer per open question above, in the same order. Omit this key entirely only if there are zero open questions." if unanswered_notes else "omit -- there are no open questions right now."}
}}
Omit "parameter_adjustments" entirely, or omit a specific field within it,
if you don't have real evidence to change it -- don't propose a change
just to have one. Proposed values for writable settings are applied
directly after strict type validation; do not include protected controls."""


def maybe_review_performance() -> None:
    """Call this periodically (screener.py's run_forever() does, once per
    screening pass) -- cheap no-op unless enough new closed trades have
    accumulated since the last review, OR there's an open user question
    waiting (2026-09-06 addition, a deliberate small difference from
    Axiom's own gating: a live question sitting unanswered for however
    many trades it takes to next close isn't a useful "communication
    channel" -- see write_user_note()'s own docstring for why this exists
    at all)."""
    memory = load_memory()
    # load_all_trades() (2026-09-11, user's explicit request: "build him
    # actual access to his full trade history... make sure all trades are
    # saved even on a dash refresh") reads trade_history_master_file, which
    # a "clear the dash" reset never touches -- unlike load_trades()'s dash
    # file, which used to mean trade_count dropped back near 0 on every
    # dashboard reset while memory["last_reviewed_trade_count"] (NOT reset
    # by a dash clear) stayed wherever it was, silently stalling this exact
    # gate below for however many trades it took to catch back up. See
    # config.py's trade_history_master_file docstring for the full finding.
    all_trades = trade_history.load_all_trades()
    trade_count = len(all_trades)
    unanswered_notes = [n for n in memory["user_notes"] if not n.get("answered")]
    if trade_count - memory["last_reviewed_trade_count"] < config.ai_review_every_n_trades and not unanswered_notes:
        return

    if not config.openai_api_key:
        log.warning("AI Free Will review due but OPENAI_API_KEY is not configured -- "
                    "skipping this review, will re-check next cycle.")
        return

    # Widened 50 -> 500 (2026-09-11, same request as above): the prompt
    # only ever sees _gather_stats()'s pre-aggregated numbers (win rate,
    # avg pnl, reason breakdown, fee-drag-by-reason), never the raw trades
    # themselves, so a much bigger window costs essentially nothing in
    # prompt size -- 500 gives real depth without reaching back so far
    # that it blends in eras from before a major tuning/code change
    # (e.g. the old 30.0-floor stale-exit era) in a way that would be
    # actively misleading for a CURRENT tuning decision.
    recent_trades = all_trades[-500:]
    autonomous_trades, manual_trades = _split_autonomous_vs_manual(recent_trades)
    stats = _gather_stats(autonomous_trades)
    manual_stats = _gather_stats(manual_trades)
    current_settings = {
        field: get_effective_value(field)
        for field in _AI_TUNABLE_FIELDS
    }
    # STUCK-diversion hook (2026-10-15): when the last ~N reviews all proposed
    # the same adjustment signature (the model's own lessons log documents
    # stretch of 10-20+ identical, often no-op, proposals), tell it to break
    # out and consider OTHER dials/directions instead of feeding it a
    # silent repeat. Distinct from the lesson-suppression gate below: that
    # one only SHORTENS logs; this actively steers the next decision.
    stuck_repeat_count = memory.get("_consecutive_repeat_count", 0)
    last_sig = memory.get("_last_adjustment_signature", [])
    stuck_warning = ""
    if stuck_repeat_count >= 3 and last_sig:
        stuck_fields = ", ".join(
            f"{pair[0]}" for pair in last_sig if isinstance(pair, (list, tuple)) and pair
        ) or "the listed settings"
        stuck_warning = (
            "\n--- STUCK-LOOP WARNING ---\n"
            f"Your last {stuck_repeat_count} reviews proposed the same adjustment(s) "
            f"[{stuck_fields}] and it repeated without meaningfully changing results.\n"
            "This is a no-op cycle -- it eats review time without exploring new ground.\n"
            "Do NOT propose that same field/direction again. Instead: pick a DIFFERENT dial, "
            "or reverse direction if the evidence clearly supports it, or if no change is "
            "genuinely well-supported, omit parameter_adjustments entirely this round rather "
            "than resubmitting a repeat. A deliberate 'no change, still observing' review is "
            "valid and better than a repeating no-op.\n"
            "---\n"
        )
    prompt = _build_prompt(stats, manual_stats, current_settings,
                           memory["lessons_learned"], unanswered_notes,
                           stuck_warning=stuck_warning)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=config.openai_api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            max_tokens=1600,
        )
        result = json.loads(response.choices[0].message.content)
    except Exception:
        log.exception("AI Free Will review call failed -- will retry next cycle, "
                       "no adjustment applied this time.")
        return

    # From here on, everything reads AND writes memory freshly under the
    # cross-process lock (2026-09-09, see config.py's ai_memory_lock_file
    # docstring) -- the `memory`/`unanswered_notes` captured above are now
    # stale: the OpenAI call just spent real, multi-second wall-clock time,
    # a long enough window for a concurrent write_user_note() call (its own
    # separate process) to have appended something this stale copy has no
    # idea about. Re-reading here and doing exactly one save at the end
    # (instead of saving this same stale object twice, once inside the old
    # _apply_user_note_answers() and again below) means a concurrent note
    # never gets silently overwritten.
    adjustments = result.get("parameter_adjustments") or {}
    if not isinstance(adjustments, dict):
        log.warning("AI returned parameter_adjustments as %s rather than an object; ignoring it.",
                    type(adjustments).__name__)
        adjustments = {}
    lesson = (result.get("lesson") or "").strip()
    with file_lock.cross_process_lock(config.ai_memory_lock_file):
        memory = load_memory()
        if unanswered_notes:
            # unanswered_notes was captured in list order before the OpenAI
            # call; user_notes is append-only and "answered" only ever goes
            # False->True, so the same notes -- in the same order -- are
            # still the first N unanswered entries in this fresh read
            # (plus possibly newer ones appended after, which we correctly
            # leave untouched for next cycle).
            fresh_unanswered = [n for n in memory["user_notes"] if not n.get("answered")]
            _apply_user_note_answers(fresh_unanswered[:len(unanswered_notes)], result.get("user_note_answers"))

        this_signature = _adjustment_signature(adjustments)
        last_signature = tuple(tuple(pair) for pair in memory.get("_last_adjustment_signature", []))
        if this_signature and this_signature == last_signature:
            repeat_count = memory.get("_consecutive_repeat_count", 0) + 1
        else:
            repeat_count = 1 if this_signature else 0
        memory["_last_adjustment_signature"] = [list(pair) for pair in this_signature]
        memory["_consecutive_repeat_count"] = repeat_count

        if lesson:
            if this_signature and repeat_count > _MAX_CONSECUTIVE_REPEATS:
                log.info("AI Free Will repeated lesson suppressed (repeat #%d, adjustment %s).",
                         repeat_count, this_signature)
                if repeat_count % 10 == 0:
                    breakthrough = (f"{_timestamp_prefix()}[REPEATED] The same adjustment "
                                    f"({this_signature}) has been proposed for {repeat_count} "
                                    f"reviews in a row; re-examine the evidence.")
                    memory["lessons_learned"].append(breakthrough)
                    memory["lessons_learned"] = memory["lessons_learned"][-20:]
                    log.warning("[bold red]AI FREE WILL STUCK[/bold red] %s", breakthrough)
            else:
                memory["lessons_learned"].append(f"{_timestamp_prefix()}{lesson}")
                memory["lessons_learned"] = memory["lessons_learned"][-20:]
                log.warning("[bold magenta]AI FREE WILL LESSON[/bold magenta] %s", lesson)

        memory["last_reviewed_trade_count"] = trade_count
        save_memory(memory)

    from bot import trading_control
    if _apply_review_adjustments(adjustments, trading_control.get_mode()):
        _start_review_pause()
