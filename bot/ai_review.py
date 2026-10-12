"""AI Free Will / self-learning review loop -- periodically reviews recent
closed trades (bot/trade_history.py) via OpenAI and may adjust
stop_loss_pct/take_profit_pct within bounded steps, writing a lesson to a
memory file. Mirrors Axiom-bot's own bot/performance_evaluator.py. Built
2026-09-06, user's own explicit request: "he needs to be fully aware and
self-learning with free will."

Adjustments are applied via config.ai_overrides_file, re-read LIVE by
bot/exit_manager.py's get_effective_stop_loss_pct()/
get_effective_take_profit_pct() every check cycle -- NOT written back into
config.py's own static fields (those are only read once at process start)
-- same reasoning Axiom-bot's own settings_override.json pattern uses:
a change takes effect immediately, no restart needed.

Deliberately a SMALLER first version than Axiom's own review loop, which
only reached its current dedup/grounding-gate/user-note sophistication
after months of live iteration (see that module's own history). This
version has the core loop -- real OpenAI call, bounded adjustment, lesson
memory -- without those later hardening layers. A natural next step once
this has run for a while and produces evidence of drift/repetition, same
as how Axiom's own gates got added.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone

import bot.file_lock as file_lock
import bot.trade_history as trade_history
from config import config

log = logging.getLogger("gmgn-agent")

# Same two fields exit_manager.py actually enforces. Bounds are a starting
# guess, not backtested -- same caveat as the base stop_loss_pct/
# take_profit_pct defaults in config.py.
_AI_FIELD_BOUNDS = {
    "stop_loss_pct": (5.0, 30.0),
    "take_profit_pct": (10.0, 200.0),
    "profit_giveback_pct": (5.0, 40.0),
    "queue_pressure_min_profit_pct": (5.0, 100.0),
    "stale_position_max_age_secs": (30.0, 1800.0),
    # Entry-selectivity fields (2026-09-08, user's explicit request: "screw
    # the selective builded so he can tune it himself" -- previously these
    # were fixed in .env with no AI-tunable path at all, which meant every
    # "I'll be more/less selective" answer the AI gave was just words with
    # no mechanism behind it (confirmed directly, real gap found live).
    # bot/security.py now reads all five via this same get_effective_*()
    # pattern the exit fields already use. Bounds keep real screening
    # intact at both ends -- 0/unlimited would mean no screening at all,
    # which is a different, much bigger decision than "be more moderate."
    "max_rug_ratio": (0.15, 0.5),
    "max_top_10_holder_rate": (0.15, 0.5),
    "max_bundler_rate": (0.15, 0.5),
    "max_rat_trader_rate": (0.1, 0.4),
    # Inverted vs the four above -- LOWER min_liquidity_usd is the LOOSER/
    # more-permissive direction (accepts thinner-liquidity tokens), higher
    # is tighter/more selective. Flagged explicitly in the prompt too so
    # the model doesn't reason about it backwards relative to the others.
    # Upper bound raised 8000 -> 12000 (2026-09-17, real evidence of
    # stop-loss drag in choppy conditions), then the WHOLE range narrowed
    # to (8000, 12000) same day after 12000 as the live value choked the
    # pipeline to zero buys (200/200 rejected, 87% on liquidity alone --
    # confirmed live, and by the AI's own answer to a direct question: "The
    # 2000 [sic, meant 12000] floor appears to be too strict ... filters
    # out nearly all freshly-deployed tokens"). Live value split the
    # difference to 10000. Lower bound raised 1000 -> 8000 too, not just
    # the upper one -- the whole point of this field going forward is
    # giving the AI room to self-correct between "too loose" (8000, where
    # it started causing the original stop-loss drag) and "too tight"
    # (12000, where it just proved it chokes the pipeline dry), not the
    # AI's full historical 1000-8000 range from before this incident.
    # Widened again same day (still 2026-09-17): testing a genuinely
    # different, more permissive approach at 3000 -- below the original
    # 8000 floor -- while tuning is manually paused. Lower bound dropped
    # to 3000 so this stays valid once tuning resumes rather than getting
    # silently clamped back up to 8000 the moment the AI next touches it.
    "min_liquidity_usd": (3000.0, 12000.0),
    # Flat-coin grace period (2026-09-10, see config.py's own field
    # comments for the full fee-drag finding this responds to). Two new
    # fields, both additive -- neither restricts anything the AI could
    # already do; they give it a genuinely new lever it didn't have before.
    # flat_pnl_band_pct bounds: 0.5 (tight -- only near-dead-flat counts)
    # to 10.0 (generous -- most small moves count as "flat").
    "flat_pnl_band_pct": (0.5, 10.0),
    # flat_position_extra_secs bounds: 0 (grace period effectively off,
    # same as today) to 900.0 (up to 15 extra minutes for a flat position
    # to develop) -- deliberately capped well below stale_position_max_age_
    # secs's own 1800.0 ceiling so "extra time" can't alone reach the kind
    # of multi-hour hold that was never the intent here.
    "flat_position_extra_secs": (0.0, 900.0),
    # Liquidity-exit floor (2026-09-14, real incident: 9 positions tonight
    # each lost 96-99.8% of their value because they kept getting held for
    # a bigger price target while the pool's own liquidity quietly drained
    # to nothing underneath them -- by the time they finally sold, there
    # was nothing left to sell into. Every existing exit rule (stop-loss,
    # take-profit, stale, etc.) is purely PRICE-based; none of them looked
    # at the pool's own liquidity at all, even though bot/exit_manager.py's
    # _fetch_price() already reads it on every single check cycle (same
    # get_token_info() call, no extra API cost). This is a genuinely new,
    # independent signal: if a position's own liquidity drops below this
    # many USD, close it immediately regardless of current PnL -- the
    # point is getting out BEFORE the pool is dead, not waiting for a
    # price target that may never be reachable once liquidity is gone.
    #
    # UNITS CORRECTED 2026-09-14: this field was initially built assuming
    # get_token_info()'s "liquidity" field was SOL-denominated -- verified
    # live against a real token (platform showed "Liq $4.02K", the raw API
    # field returned 3919.82) that it's actually USD, same convention as
    # config.min_liquidity_usd's own entry-screening check. Bounds/default
    # below are real USD figures, not the original (wrong-scale) SOL guess.
    # Tonight's dead/dying pools measured under $1 (as low as $0.04);
    # entry screening itself requires $1000-$8000 minimum just to buy.
    # $300 default sits comfortably above "already basically dead" with
    # real margin, comfortably below the entry bar so it doesn't fire the
    # instant a position is bought.
    "min_exit_liquidity_usd": (100.0, 2000.0),
}
_AI_FIELD_MAX_STEP = {
    "stop_loss_pct": 5.0,
    "take_profit_pct": 20.0,
    "profit_giveback_pct": 10.0,
    "queue_pressure_min_profit_pct": 15.0,
    "stale_position_max_age_secs": 300.0,
    "max_rug_ratio": 0.1,
    "max_top_10_holder_rate": 0.1,
    "max_bundler_rate": 0.1,
    "max_rat_trader_rate": 0.08,
    "min_liquidity_usd": 1500.0,
    "flat_pnl_band_pct": 2.0,
    "flat_position_extra_secs": 150.0,
    "min_exit_liquidity_usd": 200.0,
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


def get_effective_stop_loss_pct() -> float:
    return _load_overrides().get("stop_loss_pct", config.stop_loss_pct)


def get_effective_take_profit_pct() -> float:
    return _load_overrides().get("take_profit_pct", config.take_profit_pct)


def get_effective_profit_giveback_pct() -> float:
    return _load_overrides().get("profit_giveback_pct", config.profit_giveback_pct)


def get_effective_queue_pressure_min_profit_pct() -> float:
    return _load_overrides().get("queue_pressure_min_profit_pct", config.queue_pressure_min_profit_pct)


def get_effective_stale_position_max_age_secs() -> float:
    return _load_overrides().get("stale_position_max_age_secs", config.stale_position_max_age_secs)


def get_effective_flat_pnl_band_pct() -> float:
    return _load_overrides().get("flat_pnl_band_pct", config.flat_pnl_band_pct)


def get_effective_flat_position_extra_secs() -> float:
    return _load_overrides().get("flat_position_extra_secs", config.flat_position_extra_secs)


# -- Entry-selectivity accessors (2026-09-08) -- bot/security.py reads these
# instead of the raw config.* fields, same pattern as every exit field above.

def get_effective_max_rug_ratio() -> float:
    return _load_overrides().get("max_rug_ratio", config.max_rug_ratio)


def get_effective_max_top_10_holder_rate() -> float:
    return _load_overrides().get("max_top_10_holder_rate", config.max_top_10_holder_rate)


def get_effective_max_bundler_rate() -> float:
    return _load_overrides().get("max_bundler_rate", config.max_bundler_rate)


def get_effective_max_rat_trader_rate() -> float:
    return _load_overrides().get("max_rat_trader_rate", config.max_rat_trader_rate)


def get_effective_min_liquidity_usd() -> float:
    return _load_overrides().get("min_liquidity_usd", config.min_liquidity_usd)


def get_effective_min_exit_liquidity_usd() -> float:
    return _load_overrides().get("min_exit_liquidity_usd", config.min_exit_liquidity_usd)


# -- post-review pause (2026-09-06, user's explicit request: give the AI ---
# "time to review his data and see where the mistakes were") -------------
# Stored under a leading-underscore key in the SAME overrides file rather
# than a new one -- it's directly tied to this module's own review cycle,
# and the leading underscore keeps it out of _AI_FIELD_BOUNDS/the generic
# adjustment loop (those iterate specific known field names, never this
# key). Screening/rejection and exit management (stop-loss/take-profit/
# profit-giveback on ALREADY-open positions) are untouched by this pause --
# only bot/screener.py's _autonomous_buy() checks it, so nothing already
# open goes unmonitored while paused.

def is_paused_for_review() -> bool:
    # post_review_pause_secs <= 0 disables this entirely (2026-09-08, user's
    # explicit request -- confirmed real data first: 33 of the last 40 buy
    # attempts were skipped for this exact reason, since 5 fast closes can
    # trigger a review in well under a minute and each one then froze new
    # buys for a full 2 minutes -- the pause was eating most of his active
    # trading time). Guarded explicitly, not just left at 0 and trusted to
    # the comparison below, since time.time() + 0 is technically still a
    # valid (if near-instant) future timestamp, not a clean "disabled."
    if config.post_review_pause_secs <= 0:
        return False
    until = _load_overrides().get("_review_pause_until", 0.0)
    return time.time() < until


def _start_review_pause() -> None:
    if config.post_review_pause_secs <= 0:
        return
    with file_lock.cross_process_lock(config.ai_overrides_lock_file):
        overrides = _load_overrides()
        overrides["_review_pause_until"] = time.time() + config.post_review_pause_secs
        _atomic_write(config.ai_overrides_file, overrides)
    log.warning("[bold magenta]AI FREE WILL[/bold magenta] pausing new buys for %.0fs to let this "
                "review's adjustments take effect before more trades pile up.", config.post_review_pause_secs)


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


def _apply_adjustment(field: str, proposed: float) -> float | None:
    """Clamp `proposed` into field's bounds and max-step-per-adjustment,
    same two-guardrail shape Axiom's own auto_tuner.py uses. Returns the
    actual applied value, or None if nothing changed (already at the
    clamped value).

    Cross-process lock (2026-09-09, see config.py's ai_overrides_lock_file
    docstring): Claude's own ad-hoc `python -c` interventions on this exact
    file happened repeatedly tonight, concurrently with this exact function
    potentially running in the live bot's own process."""
    with file_lock.cross_process_lock(config.ai_overrides_lock_file):
        lo, hi = _AI_FIELD_BOUNDS[field]
        max_step = _AI_FIELD_MAX_STEP[field]
        overrides = _load_overrides()
        current = overrides.get(field, getattr(config, field))

        step = max(-max_step, min(max_step, proposed - current))
        new_value = max(lo, min(hi, current + step))
        if new_value == current:
            return None

        overrides[field] = new_value
        _atomic_write(config.ai_overrides_file, overrides)
        return new_value


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
    """(field, '+'/'-') pairs for every field this review proposed
    adjusting, direction relative to its CURRENT effective value at call
    time -- sorted so the signature doesn't depend on which order the
    model listed fields in. Empty tuple if nothing was proposed (never
    counts as a "repeat" of anything)."""
    sig = []
    for field in _AI_FIELD_BOUNDS:
        if field not in adjustments:
            continue
        try:
            proposed = float(adjustments[field])
        except (TypeError, ValueError):
            continue
        current = _load_overrides().get(field, getattr(config, field))
        if proposed > current:
            sig.append((field, "+"))
        elif proposed < current:
            sig.append((field, "-"))
    return tuple(sorted(sig))


def save_memory(memory: dict) -> None:
    _atomic_write(config.ai_memory_file, memory)


def write_user_note(text: str) -> None:
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
    be in flight in that other process at the exact same moment."""
    with file_lock.cross_process_lock(config.ai_memory_lock_file):
        memory = load_memory()
        memory["user_notes"].append({
            "ts": _timestamp_prefix().strip(" []"),
            "text": text,
            "answered": False,
            "answer": None,
        })
        save_memory(memory)


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
    the model to infer, same reasoning as _bound_flag() below (an implicit
    signal in the raw numbers wasn't enough on its own to change behavior,
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


def _bound_flag(value: float, lo: float, hi: float) -> str:
    """Explicit, hard-to-miss text flag for when a field is already sitting
    on one of its bounds -- built 2026-09-10 after finding a real bug live:
    the CURRENT SETTINGS block below already showed "(bounds: lo-hi)" next
    to each field, but never said in words that a field's current value
    WAS one of those bounds already. gpt-4o-mini kept proposing to push
    stale_position_max_age_secs below its own floor of 30.0 for 150+
    consecutive review cycles -- _apply_adjustment() correctly clamped
    every one of those to a no-op, but the model was never told in the
    prompt that the move was already impossible, so it kept "discovering"
    the same dead end from the raw numbers alone. This makes it
    unmissable instead of inferable."""
    if value <= lo:
        return " *** AT MINIMUM -- cannot go any lower, do not propose decreasing this ***"
    if value >= hi:
        return " *** AT MAXIMUM -- cannot go any higher, do not propose increasing this ***"
    return ""


def _build_prompt(stats: dict, manual_stats: dict, current_stop_loss: float, current_take_profit: float,
                   current_giveback: float, current_queue_pressure: float, current_stale_age: float,
                   current_max_rug_ratio: float, current_max_top10: float, current_max_bundler: float,
                   current_max_rat: float, current_min_liquidity: float,
                   current_flat_band: float, current_flat_extra: float,
                   current_min_exit_liquidity: float,
                   recent_lessons: list[str], unanswered_notes: list[dict]) -> str:
    sl_lo, sl_hi = _AI_FIELD_BOUNDS["stop_loss_pct"]
    tp_lo, tp_hi = _AI_FIELD_BOUNDS["take_profit_pct"]
    gb_lo, gb_hi = _AI_FIELD_BOUNDS["profit_giveback_pct"]
    qp_lo, qp_hi = _AI_FIELD_BOUNDS["queue_pressure_min_profit_pct"]
    st_lo, st_hi = _AI_FIELD_BOUNDS["stale_position_max_age_secs"]
    rr_lo, rr_hi = _AI_FIELD_BOUNDS["max_rug_ratio"]
    t10_lo, t10_hi = _AI_FIELD_BOUNDS["max_top_10_holder_rate"]
    bu_lo, bu_hi = _AI_FIELD_BOUNDS["max_bundler_rate"]
    rat_lo, rat_hi = _AI_FIELD_BOUNDS["max_rat_trader_rate"]
    liq_lo, liq_hi = _AI_FIELD_BOUNDS["min_liquidity_usd"]
    fb_lo, fb_hi = _AI_FIELD_BOUNDS["flat_pnl_band_pct"]
    fe_lo, fe_hi = _AI_FIELD_BOUNDS["flat_position_extra_secs"]
    mel_lo, mel_hi = _AI_FIELD_BOUNDS["min_exit_liquidity_usd"]
    lessons_block = "\n".join(f"- {l}" for l in recent_lessons[-5:]) or "(none yet)"
    notes_section = ""
    if unanswered_notes:
        questions = "\n".join(f"{i}. {n['text']}" for i, n in enumerate(unanswered_notes, start=1))
        notes_section = f"""

--- OPEN QUESTIONS FROM THE USER ({len(unanswered_notes)} unanswered) ---
These are NOT lessons already logged -- they are live questions/directives a
human wrote to you directly and is waiting on. Answer EVERY one of them in
"user_note_answers" below, in the same order. If you genuinely don't have
the data to answer one specifically, say exactly that rather than a vague
restatement -- that IS a valid, complete answer.
{questions}"""
    return f"""You are the autonomous trading-strategy reviewer for a Solana memecoin
trading bot (GMGN platform). It buys tokens the instant they pass a
security screen, with NO human approval per trade -- but it IS capped to
max_concurrent_positions open positions at once (a pure capital-exposure
cap, not a quality filter). You have TWELVE tunable levers now: five exit
controls (stop_loss_pct, take_profit_pct, profit_giveback_pct,
queue_pressure_min_profit_pct, stale_position_max_age_secs), five ENTRY-
selectivity controls (max_rug_ratio, max_top_10_holder_rate,
max_bundler_rate, max_rat_trader_rate, min_liquidity_usd, added
2026-09-08), two FLAT-COIN GRACE PERIOD controls (flat_pnl_band_pct,
flat_position_extra_secs, added 2026-09-10), and one LIQUIDITY-EXIT
control (min_exit_liquidity_usd, added 2026-09-14 -- closes a position
immediately regardless of PnL once its own pool liquidity drops too low,
the one exit trigger that isn't purely price-based) -- these are the only
risk/exit/entry controls in the whole system. Take this seriously.

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

ENTRY-SELECTIVITY FIELDS (new 2026-09-08 -- before this, "being more or
less selective" had NO real mechanism behind it; these five are that
mechanism now, so back any change here with the same real evidence you'd
use for an exit-side change):
- max_rug_ratio / max_top_10_holder_rate / max_bundler_rate /
  max_rat_trader_rate: each candidate is REJECTED if the matching field on
  it exceeds this value. LOWER = tighter/more selective (rejects more
  candidates); HIGHER = looser/more moderate (accepts more candidates).
- min_liquidity_usd: candidate is rejected if its liquidity is BELOW this.
  This one is INVERTED versus the four above -- LOWER = looser/more
  permissive (accepts thinner-liquidity tokens), HIGHER = tighter/more
  selective. Don't reason about it the same direction as the other four.
- These five do NOT touch the fixed scam-detection gates (honeypot check,
  non-zero buy/sell tax, mint/freeze-authority renouncement) -- those stay
  fixed regardless of what you propose here; they're not "selectivity",
  they're baseline scam protection.

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

CURRENT SETTINGS (read the AT MINIMUM/AT MAXIMUM flags below literally --
if a field has one, any proposal in that blocked direction is silently
clamped to a no-op, it will NOT take effect no matter how you justify it):
- stop_loss_pct: {current_stop_loss} (bounds: {sl_lo}-{sl_hi}){_bound_flag(current_stop_loss, sl_lo, sl_hi)}
- take_profit_pct: {current_take_profit} (bounds: {tp_lo}-{tp_hi}){_bound_flag(current_take_profit, tp_lo, tp_hi)}
- profit_giveback_pct: {current_giveback} (bounds: {gb_lo}-{gb_hi}){_bound_flag(current_giveback, gb_lo, gb_hi)}
- queue_pressure_min_profit_pct: {current_queue_pressure} (bounds: {qp_lo}-{qp_hi}){_bound_flag(current_queue_pressure, qp_lo, qp_hi)}
- stale_position_max_age_secs: {current_stale_age} (bounds: {st_lo}-{st_hi}){_bound_flag(current_stale_age, st_lo, st_hi)}
- max_rug_ratio: {current_max_rug_ratio} (bounds: {rr_lo}-{rr_hi}, lower = more selective){_bound_flag(current_max_rug_ratio, rr_lo, rr_hi)}
- max_top_10_holder_rate: {current_max_top10} (bounds: {t10_lo}-{t10_hi}, lower = more selective){_bound_flag(current_max_top10, t10_lo, t10_hi)}
- max_bundler_rate: {current_max_bundler} (bounds: {bu_lo}-{bu_hi}, lower = more selective){_bound_flag(current_max_bundler, bu_lo, bu_hi)}
- max_rat_trader_rate: {current_max_rat} (bounds: {rat_lo}-{rat_hi}, lower = more selective){_bound_flag(current_max_rat, rat_lo, rat_hi)}
- min_liquidity_usd: {current_min_liquidity} (bounds: {liq_lo}-{liq_hi}, HIGHER = more selective -- inverted){_bound_flag(current_min_liquidity, liq_lo, liq_hi)}
- flat_pnl_band_pct: {current_flat_band} (bounds: {fb_lo}-{fb_hi}, how close to 0% pnl counts as "flat"){_bound_flag(current_flat_band, fb_lo, fb_hi)}
- flat_position_extra_secs: {current_flat_extra} (bounds: {fe_lo}-{fe_hi}, extra time a flat position gets before the stale clock; 0 = off){_bound_flag(current_flat_extra, fe_lo, fe_hi)}
- min_exit_liquidity_usd: ${current_min_exit_liquidity} (bounds: ${mel_lo}-${mel_hi}, position closes IMMEDIATELY regardless of PnL once its own pool liquidity drops below this many USD -- HIGHER = more protective/cautious, LOWER = more risk tolerance for thin pools){_bound_flag(current_min_exit_liquidity, mel_lo, mel_hi)}

If a field is stuck AT MINIMUM or AT MAXIMUM and you believe it's still the
right lever, say so explicitly in your lesson (e.g. "X is at its bound and
should be widened") instead of silently re-proposing the same blocked
value -- that is real, actionable information for the user, distinct from
just repeating the same impossible request.

RECENT LESSONS YOU'VE ALREADY LEARNED (don't just repeat these):
{lessons_block}
{notes_section}

Respond with a JSON object:
{{
  "lesson": "one sentence, what you learned from this batch of trades -- be
    specific and grounded in the actual numbers above, not generic advice",
  "parameter_adjustments": {{
    "stop_loss_pct": <float, optional, only if you have real evidence for a
      different value>,
    "take_profit_pct": <float, optional, only if you have real evidence for
      a different value>,
    "profit_giveback_pct": <float, optional, only if you have real evidence
      for a different value>,
    "queue_pressure_min_profit_pct": <float, optional, only if you have real
      evidence for a different value>,
    "stale_position_max_age_secs": <float, optional, only if you have real
      evidence for a different value>,
    "max_rug_ratio": <float, optional, only if you have real evidence for a
      different value -- lower = more selective>,
    "max_top_10_holder_rate": <float, optional, same as above -- lower = more
      selective>,
    "max_bundler_rate": <float, optional, same as above -- lower = more
      selective>,
    "max_rat_trader_rate": <float, optional, same as above -- lower = more
      selective>,
    "min_liquidity_usd": <float, optional, INVERTED -- higher = more
      selective, only if you have real evidence for a different value>,
    "flat_pnl_band_pct": <float, optional, only if you have real evidence
      for a different value -- how close to 0% pnl counts as "flat">,
    "flat_position_extra_secs": <float, optional, only if you have real
      evidence for a different value -- 0 disables the grace period>,
    "min_exit_liquidity_usd": <float, optional, only if you have real
      evidence for a different value -- HIGHER = closes positions sooner
      when liquidity is thinning, LOWER = tolerates thinner pools longer>
  }},
  "user_note_answers": {"REQUIRED -- a list of exactly " + str(len(unanswered_notes)) + " strings, one direct answer per open question above, in the same order. Omit this key entirely only if there are zero open questions." if unanswered_notes else "omit -- there are no open questions right now."}
}}
Omit "parameter_adjustments" entirely, or omit a specific field within it,
if you don't have real evidence to change it -- don't propose a change
just to have one. Any adjustment is clamped to a small step and to fixed
bounds regardless of what you propose, so propose your honest best value,
not an exaggerated one to compensate for expected clamping."""


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
    current_stop_loss = get_effective_stop_loss_pct()
    current_take_profit = get_effective_take_profit_pct()
    current_giveback = get_effective_profit_giveback_pct()
    current_queue_pressure = get_effective_queue_pressure_min_profit_pct()
    current_stale_age = get_effective_stale_position_max_age_secs()
    current_max_rug_ratio = get_effective_max_rug_ratio()
    current_max_top10 = get_effective_max_top_10_holder_rate()
    current_max_bundler = get_effective_max_bundler_rate()
    current_max_rat = get_effective_max_rat_trader_rate()
    current_min_liquidity = get_effective_min_liquidity_usd()
    current_flat_band = get_effective_flat_pnl_band_pct()
    current_flat_extra = get_effective_flat_position_extra_secs()
    current_min_exit_liquidity = get_effective_min_exit_liquidity_usd()
    prompt = _build_prompt(stats, manual_stats, current_stop_loss, current_take_profit, current_giveback,
                            current_queue_pressure, current_stale_age, current_max_rug_ratio, current_max_top10,
                            current_max_bundler, current_max_rat, current_min_liquidity,
                            current_flat_band, current_flat_extra, current_min_exit_liquidity,
                            memory["lessons_learned"], unanswered_notes)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=config.openai_api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            # Raised 300->500 (2026-09-06, same reasoning as Axiom's own
            # 400->600 bump): answering open user questions on top of the
            # usual lesson/adjustment fields needs more room than the
            # original single-lesson budget allowed for.
            max_tokens=500,
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
                # Real bug found live 2026-09-10: this branch's old log message
                # claimed "the adjustment below still applies, just not logged
                # again" -- false for any field already at a bound, since
                # _apply_adjustment() clamps to a no-op there. That let a stuck
                # repeat (proposing to push stale_position_max_age_secs below
                # its own 30.0 floor) run 150+ cycles completely invisibly --
                # never applied, never logged, the model just quietly stuck.
                # _build_prompt()'s new AT MINIMUM/AT MAXIMUM flags are the
                # real fix (tell the model up front so it stops proposing the
                # impossible move), but this is the safety net in case a
                # model still doesn't listen: surface a short, distinct
                # breakthrough notice periodically instead of suppressing
                # forever, so a stuck loop stays visible to the user even if
                # the root-cause fix above doesn't fully hold.
                log.info("AI Free Will lesson suppressed (repeat #%d of the same conclusion %s) -- "
                          "not necessarily applied; may be a no-op if a field is at its bound.",
                          repeat_count, this_signature)
                if repeat_count % 10 == 0:
                    breakthrough = (f"{_timestamp_prefix()}[STUCK] Still proposing the same "
                                     f"adjustment ({this_signature}) for {repeat_count} reviews in a "
                                     f"row -- this may be a no-op (field at its bound) that never "
                                     f"actually applies. Re-examine other fields/evidence.")
                    memory["lessons_learned"].append(breakthrough)
                    memory["lessons_learned"] = memory["lessons_learned"][-20:]
                    log.warning("[bold red]AI FREE WILL STUCK[/bold red] %s", breakthrough)
            else:
                memory["lessons_learned"].append(f"{_timestamp_prefix()}{lesson}")
                memory["lessons_learned"] = memory["lessons_learned"][-20:]
                log.warning("[bold magenta]AI FREE WILL LESSON[/bold magenta] %s", lesson)

        memory["last_reviewed_trade_count"] = trade_count
        save_memory(memory)

    if is_tuning_paused():
        log.info("AI Free Will tuning is manually paused (operator request) -- review ran "
                  "(lessons/notes recorded above), but skipping every proposed parameter "
                  "adjustment this cycle.")
    else:
        for field in ("stop_loss_pct", "take_profit_pct", "profit_giveback_pct",
                      "queue_pressure_min_profit_pct", "stale_position_max_age_secs",
                      "max_rug_ratio", "max_top_10_holder_rate", "max_bundler_rate",
                      "max_rat_trader_rate", "min_liquidity_usd",
                      "flat_pnl_band_pct", "flat_position_extra_secs", "min_exit_liquidity_usd"):
            if field in adjustments:
                try:
                    proposed = float(adjustments[field])
                except (TypeError, ValueError):
                    continue
                applied = _apply_adjustment(field, proposed)
                if applied is not None:
                    log.warning("[bold magenta]AI FREE WILL ADJUSTMENT[/bold magenta] %s -> %.2f "
                                "(proposed %.2f)", field, applied, proposed)

    _start_review_pause()
