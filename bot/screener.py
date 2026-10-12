"""Core screening loop: continuously polls GMGN's trenches endpoint for
newly-created tokens, runs both security-audit passes (bot/security.py),
and -- as of 2026-09-06, user's explicit direction to make this fully
autonomous, matching Axiom-bot's own no-human-in-the-loop trading model --
buys the INSTANT a candidate clears, no staging, no approval step. The
security screen (bot/security.py) is the only gate on what gets bought;
there is deliberately no entry-side risk limit (no daily loss cap, no
position-count cap, no cooldown) per that same direction. Exit management
(stop-loss/take-profit) lives in bot/exit_manager.py, run alongside this
loop -- see run_forever() in main.py-equivalent role here, since GMGN-bot
has no separate main.py (this loop IS the engine, same reasoning
tui.py's own docstring gives for why it's the closest analog).

cleared_candidates.json is kept as a trade-history log (what got bought,
when, at what mode/result) -- NOT a pending-approval queue anymore. The
"status" field's old "pending"/"approved"/"rejected" meaning is retired;
it now reads "bought" / "paper_bought" / "buy_failed" / "skipped_off".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Set

import bot.ai_review as ai_review
import bot.executor as executor
import bot.file_lock as file_lock
import bot.gmgn_client as gmgn
import bot.paper_trader as paper_trader
import bot.rate_limiter as rate_limiter
import bot.security as security
import bot.trading_control as trading_control
from config import config

log = logging.getLogger("gmgn-agent")


def _load_seen_mints() -> Set[str]:
    if not os.path.exists(config.seen_mints_file):
        return set()
    try:
        with open(config.seen_mints_file, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        log.exception("Failed to load seen-mints file -- starting empty (will re-screen "
                       "already-seen tokens once, harmless, just wasted API calls).")
        return set()


def _save_seen_mints(seen: Set[str]) -> None:
    """Atomic write, same temp-file-then-os.replace pattern Axiom-bot's own
    bot/overrides.py uses -- a mid-write crash must never leave this file
    truncated/corrupt, since it's read back on every restart."""
    try:
        os.makedirs(os.path.dirname(config.seen_mints_file) or ".", exist_ok=True)
        # Cap at a reasonable size so this can't grow forever across a
        # long-running process -- keep the most recent 5000, same
        # bounded-memory reasoning as Axiom-bot's own _SEEN_MAX_SIZE.
        trimmed = list(seen)[-5000:]
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(config.seen_mints_file) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(trimmed, f)
        os.replace(tmp, config.seen_mints_file)
    except Exception:
        log.exception("Failed to save seen-mints file -- next poll may re-screen a few tokens, harmless.")


def _append_trade_log(entry: dict) -> None:
    """Append one autonomous-buy outcome to the trade-history log. Same
    atomic-write pattern as _save_seen_mints() above, plus a cross-process
    lock (2026-10 latency audit) around the whole read-modify-write --
    screening can clear more than one candidate in close succession, and
    without it two concurrent appends could each read the same `existing`
    list and the second os.replace() would silently drop the first one's
    entry, same lost-write race bot/file_lock.py's own docstring
    describes for paper_portfolio.json/live_positions.json."""
    try:
        os.makedirs(os.path.dirname(config.cleared_candidates_file) or ".", exist_ok=True)
        with file_lock.cross_process_lock(config.cleared_candidates_lock_file):
            existing = []
            if os.path.exists(config.cleared_candidates_file):
                try:
                    with open(config.cleared_candidates_file, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    log.exception("Trade-history file unreadable -- starting a fresh list "
                                   "rather than blocking this record.")
                    existing = []
            existing.append(entry)
            existing = existing[-1000:]  # bounded, same reasoning as seen-mints above
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(config.cleared_candidates_file) or ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
            os.replace(tmp, config.cleared_candidates_file)
    except Exception:
        log.exception("Failed to record trade-history entry for %s.", entry.get("symbol"))


async def _autonomous_buy(token: dict, trenches_result: security.SecurityResult,
                           security_result: security.SecurityResult, source: str = "screening") -> None:
    """Buys the instant a candidate clears security screening -- no human
    approval step (2026-09-06, user's explicit direction: fully autonomous,
    matching Axiom-bot's own model). trading_control's mode is still the
    one master on/off/paper/live switch -- "autonomous" means no per-trade
    click, not no way to pause the whole system, same relationship Axiom's
    own main.py has with its MODE/switch.json.

    `source` (2026-09-15) -- which signal found this candidate: "screening"
    (this module's own trenches scan, the default), "wallet_follow"
    (bot/wallet_follow.py mirroring a followed wallet's trades),
    "dev_snipe" (bot/dev_snipe.py buying a watched dev's fresh deployment),
    "snipex" (bot/snipex.py buying a contract address posted by a watched
    X account), "token_snipe" (bot/token_snipe.py buying a specific,
    user-given token address the instant it qualifies), or "skyeye_copy"
    (bot/skyeye_copy.py buying whatever GMGN's own curated smart-money
    feed is opening/adding to). This is the ONE shared entry point every signal source buys through -- same risk
    gates (mode/time-pause/review-pause/burst-cooldown/min-interval/max-
    concurrent-positions), same paper/live execution, same exit management
    afterward, regardless of source. User's explicit architecture call
    2026-09-15: one unified engine, buckets are signal sources only, not
    separate traders with separate rules -- see bot/wallet_follow.py's own
    module docstring for the full reasoning. `source` only ever affects the
    activity-log entry and the position's own "source" field (reporting),
    never which gates apply or how the buy executes."""
    address = token.get("address")
    symbol = token.get("symbol")
    mode = trading_control.get_mode()
    entry = {
        "ts": time.time(),
        "address": address,
        "symbol": symbol,
        "name": token.get("name"),
        "market_cap": token.get("market_cap"),
        "liquidity": token.get("liquidity"),
        "holder_count": token.get("holder_count"),
        "rug_ratio": token.get("rug_ratio"),
        "top_10_holder_rate": token.get("top_10_holder_rate"),
        "gmgn_link": f"https://gmgn.ai/{config.chain}/token/{address}",
        "trenches_passed": trenches_result.passed,
        "security_passed": security_result.passed,
        "source": source,
    }
    token = {**token, "source": source}

    if mode == "off":
        entry["status"] = "skipped_off"
        _append_trade_log(entry)
        log.info("CLEARED %s (%s) -- trading mode is off, not buying.", symbol, address)
        return

    if trading_control.is_time_paused():
        entry["status"] = "skipped_time_pause"
        _append_trade_log(entry)
        log.info("CLEARED %s (%s) -- time-of-day pause active (confirmed-bad hour, see "
                  "SCHEDULE.md), not buying (resumes automatically once the window passes).",
                  symbol, address)
        return

    if ai_review.is_paused_for_review():
        entry["status"] = "skipped_review_pause"
        _append_trade_log(entry)
        log.info("CLEARED %s (%s) -- AI Free Will review pause active, not buying yet.", symbol, address)
        return

    if mode == "live" and trading_control.is_live_session_paused():
        entry["status"] = "skipped_live_session_paused"
        _append_trade_log(entry)
        log.info("CLEARED %s (%s) -- live session trade cap reached, not buying "
                  "(mode stays live so open positions keep normal exit protection).", symbol, address)
        return

    # Rolling trade-burst cooldown (2026-09-07, see config.py's
    # trade_burst_limit docstring) -- mode-agnostic, unlike the live-only
    # check above, and auto-resumes on its own once the cooldown elapses.
    if trading_control.is_burst_paused():
        entry["status"] = "skipped_burst_cooldown"
        _append_trade_log(entry)
        log.info("CLEARED %s (%s) -- trade burst cooldown active (%.0fs remaining), not buying.",
                  symbol, address, trading_control.burst_cooldown_remaining())
        return

    # Minimum time between trades (2026-09-09, see config.py's
    # min_time_between_trades_secs docstring) -- a flat frequency throttle,
    # separate from the burst cooldown above (that one only kicks in every
    # N trades; this one applies to EVERY trade, closing the fee-drag gap
    # entry-selectivity alone couldn't safely fix).
    if trading_control.is_min_interval_active():
        entry["status"] = "skipped_min_interval"
        _append_trade_log(entry)
        log.info("CLEARED %s (%s) -- minimum time between trades not yet elapsed (%.1fs remaining), not buying.",
                  symbol, address, trading_control.min_interval_remaining())
        return

    # Max concurrent positions (2026-09-06, user's explicit request: "only
    # execute 3 trades at one single time... that way he doesn't burn
    # through the wallet that fast"). Counts whichever mode is currently
    # active -- paper and live never both hold real positions at once
    # since only one mode is active at a time.
    open_count = (len(paper_trader.get_portfolio()["positions"]) if mode == "paper"
                  else len(executor.get_positions()["positions"]))
    if open_count >= config.max_concurrent_positions:
        entry["status"] = "skipped_max_positions"
        _append_trade_log(entry)
        log.info("CLEARED %s (%s) -- already at max_concurrent_positions (%d), not buying.",
                  symbol, address, config.max_concurrent_positions)
        return

    if mode == "paper":
        try:
            position = await asyncio.to_thread(paper_trader.simulate_buy, token)
            entry["status"] = "paper_bought"
            entry["entry_price"] = position["entry_price"]
            trading_control.record_trade_for_burst()
            trading_control.record_trade_time()
            log.info("[bold cyan]AUTONOMOUS PAPER BUY[/bold cyan] %s (%s) @ %.10f",
                      symbol, address, position["entry_price"])
        except (paper_trader.PriceUnavailable, paper_trader.PositionError) as exc:
            entry["status"] = "buy_failed"
            entry["error"] = str(exc)
            log.warning("Paper buy failed for %s: %s", symbol, exc)
        _append_trade_log(entry)
        return

    # mode == "live" -- real funds. execute_buy() still independently
    # requires GMGN_ALLOW_AUTOMATED_TRADES=1 (bot/executor.py's own
    # fail-closed check) -- this loop never sets that itself.
    try:
        position = await asyncio.to_thread(executor.execute_buy, token)
        entry["status"] = "bought"
        entry["buy_result"] = position.get("buy_result")
        trading_control.record_trade_for_burst()
        trading_control.record_trade_time()
        log.warning("[bold red]AUTONOMOUS LIVE BUY[/bold red] %s (%s) -- REAL FUNDS.", symbol, address)
        rate_limiter.reset()
    except executor.ExecutionBlocked as exc:
        entry["status"] = "buy_failed"
        entry["error"] = str(exc)
        log.warning("Live buy blocked for %s: %s", symbol, exc)
    except gmgn.GmgnRateLimitError as exc:
        # Same rate-limit path as the token-security check above -- this is
        # exactly what happened to the real "LEAP" incident 2026-09-06: a
        # live swap attempt hit GMGN's 429 ban. Back off the same way
        # rather than just recording a generic failure and moving on
        # immediately, which would just re-hit the same ban on the very
        # next candidate.
        entry["status"] = "buy_failed"
        entry["error"] = str(exc)
        log.warning("Live buy for %s hit GMGN's rate limit.", symbol)
        await rate_limiter.handle_hit(exc.retry_after_secs)
    except gmgn.GmgnCliError as exc:
        # Added 2026-09-06 after a real production hang: this branch
        # previously only caught ExecutionBlocked, so a gmgn-cli failure
        # (a timeout, a bad response, anything from bot/gmgn_client.py's
        # run()) propagated all the way out of this function, out of
        # screen_once()'s per-token loop (no try/except there), and only
        # got caught by _screen_loop()'s own broad handler -- which meant
        # ONE failed live buy silently skipped every OTHER candidate still
        # left in that same screening batch, not just this one. Catching
        # it here means a single bad live-buy attempt is recorded and
        # logged on its own, and screening continues to the next candidate
        # in this same pass instead of aborting the whole batch.
        entry["status"] = "buy_failed"
        entry["error"] = str(exc)
        log.exception("Live buy failed for %s (gmgn-cli error): %s", symbol, exc)
    except Exception:  # noqa: BLE001 -- see comment above: one bad live buy must
                       # never take the rest of this screening pass down with it
        entry["status"] = "buy_failed"
        entry["error"] = "unexpected exception -- see log"
        log.exception("Live buy raised an unexpected exception for %s -- recorded as failed, "
                       "continuing to the next candidate.", symbol)
    _append_trade_log(entry)


def _append_activity(token: dict, verdict: str, reasons: list[str] | None = None) -> None:
    """Append one screening decision (cleared OR rejected) to a bounded
    rolling feed the dashboard can display -- same atomic-write pattern as
    every other staging file here. Separate from cleared_candidates.json
    (which ONLY ever holds cleared entries pending human review) -- this
    file exists purely for visibility into what the screener is doing,
    nothing reads it back into a trading decision."""
    entry = {
        "ts": time.time(),
        "symbol": token.get("symbol"),
        "address": token.get("address"),
        "verdict": verdict,  # "cleared" | "rejected"
        "reasons": reasons or [],
        # 2026-09-16: how long screening's own detection-to-decision pipeline
        # took for this candidate. NOT the same as latency_ms in
        # trade_history.json -- this fires here for a "cleared" candidate
        # BEFORE _autonomous_buy() actually attempts the buy, so there is no
        # entry_ts/order-completion time to compute a full latency_ms from
        # yet at this point. None for a candidate that never got a
        # trigger_ts (nothing currently calls this without one, but fail
        # soft rather than KeyError if that ever changes).
        "trigger_ts": token.get("trigger_ts"),
    }
    try:
        os.makedirs(os.path.dirname(config.activity_log_file) or ".", exist_ok=True)
        # Cross-process lock (2026-10 latency audit, same reasoning as
        # _append_trade_log() above) -- concurrent cleared/rejected
        # candidates in the same screening pass would otherwise race on
        # this same read-modify-write and silently drop one entry.
        with file_lock.cross_process_lock(config.activity_log_lock_file):
            existing = []
            if os.path.exists(config.activity_log_file):
                try:
                    with open(config.activity_log_file, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                except Exception:
                    existing = []
            existing.append(entry)
            existing = existing[-200:]  # bounded rolling feed, same reasoning as seen-mints/cleared-candidates
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(config.activity_log_file) or ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
            os.replace(tmp, config.activity_log_file)
    except Exception:
        log.exception("Failed to append activity entry for %s -- dashboard visibility only, "
                       "no effect on the actual screening decision already made.", token.get("symbol"))


# Rate-limit handling moved to bot/rate_limiter.py (2026-09-06) -- SHARED
# state, not screener.py-private, since bot/exit_manager.py's own price
# checks hit the same GMGN account/IP-level ban and need to back off in
# coordination with this loop, not track it independently.


async def screen_once(seen: Set[str]) -> int:
    """One screening pass: fetch trending candidates, run the fast
    trenches-field check first (no extra API call), only call
    get_token_security() (a real extra HTTP round-trip) for candidates
    that already cleared the free first pass -- avoids spending API calls
    auditing tokens that were always going to fail the cheap check anyway.
    Returns how many NEW (not-yet-seen) tokens were evaluated this pass.

    SWITCHED from trenches (new_creation + completed) to `market trending`
    (2026-09-19, user's explicit direction after watching live charts:
    "stop him from buying from the trenches because that area is brutal...
    98% of the tokens never make it. I mush rather buying from trending").
    Trending tokens range roughly 1 minute to 1 day old (user's own
    observation, confirmed against real data) -- already-proven survivors
    with real holder/volume history, a fundamentally different risk profile
    than a brand-new deploy. bot/security.py's check_trenches_fields() was
    updated alongside this change -- net_buy_24h/buys_24h are trenches-only
    fields, confirmed absent entirely from a real get_trending() response
    (not null, not present -- same failure shape bundler_rate had 2026-09-06),
    so that check is now key-presence-aware rather than fail-closed on a
    field this endpoint never returns. Every other field the security
    pipeline reads (rug_ratio, top_10_holder_rate, liquidity, market_cap,
    is_wash_trading, renounced_mint/renounced_freeze_account) confirmed
    present with the same names/types on trending's real response.
    new_creation_max_age no longer applies (trending has no equivalent
    filter) -- left defined in config.py, unused, same precedent as
    min_holder_count."""
    candidates: list = []
    try:
        candidates = await asyncio.to_thread(
            gmgn.get_trending, chain=config.chain, interval="1h", limit=80)
        rate_limiter.reset()
    except gmgn.GmgnRateLimitError as exc:
        log.warning("Trending poll hit GMGN's rate limit.")
        await rate_limiter.handle_hit(exc.retry_after_secs)
    except gmgn.GmgnCliError:
        log.exception("Trending poll failed -- one bad poll must not kill this loop.")

    new_count = 0
    for token in candidates:
        address = token.get("address")
        if not address or address in seen:
            continue
        seen.add(address)
        new_count += 1

        # Signal-to-buy latency instrumentation (2026-09-16, user + son's
        # explicit request, Axiom-bot's own trigger_ts pattern) -- stamped
        # the exact moment this candidate is first seen as NEW, before any
        # of the filtering/API-call work below. Carried through `token`
        # (never stripped by check_trenches_fields/check_token_security,
        # which only ever read fields off it) all the way into
        # paper_trader.simulate_buy()/executor.execute_buy(), which compute
        # the actual entry_ts - trigger_ts delta once a buy really happens.
        token["trigger_ts"] = time.time()

        trenches_result = security.check_trenches_fields(token)
        if not trenches_result.passed:
            log.info("REJECTED %s (cheap check): %s", token.get("symbol"), trenches_result.reasons)
            _append_activity(token, "rejected", trenches_result.reasons)
            continue

        # Inter-request throttling (2026-09-06): a brief, deliberate pause
        # before EVERY real token-security call, not just after a failure --
        # with the bundler_rate fix now letting ~22 candidates/poll clear the
        # cheap check, spamming that many real API calls back-to-back is
        # exactly what triggered GMGN's rate limit in the first place.
        await asyncio.sleep(config.token_security_throttle_secs)
        try:
            token_security = await asyncio.to_thread(gmgn.get_token_security, address, chain=config.chain)
            rate_limiter.reset()
        except gmgn.GmgnRateLimitError as exc:
            log.warning("token security check for %s hit GMGN's rate limit.", token.get("symbol"))
            _append_activity(token, "rejected", ["rate-limited (fail-closed)"])
            await rate_limiter.handle_hit(exc.retry_after_secs)
            continue
        except gmgn.GmgnCliError:
            log.exception("token security check failed for %s -- treating as a reject "
                          "(fail-closed: a check we couldn't run is not a passed check).",
                          token.get("symbol"))
            _append_activity(token, "rejected", ["security check API call failed (fail-closed)"])
            continue
        security_result = security.check_token_security(token_security)
        if not security_result.passed:
            log.info("REJECTED %s (security check): %s", token.get("symbol"), security_result.reasons)
            _append_activity(token, "rejected", security_result.reasons)
            continue

        _append_activity(token, "cleared")
        await _autonomous_buy(token, trenches_result, security_result)

    return new_count


async def _screen_loop() -> None:
    seen = _load_seen_mints()
    log.info("Screener starting -- chain=%s poll_interval=%.1fs, %d already-seen mint(s) loaded.",
              config.chain, config.poll_interval_secs, len(seen))
    while True:
        try:
            new_count = await screen_once(seen)
            if new_count:
                _save_seen_mints(seen)
                log.info("Screening pass complete: %d new token(s) evaluated.", new_count)
            trading_control.maybe_log_live_session_pause()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad pass must never kill the whole loop
            log.exception("Screening pass crashed -- continuing on the next poll.")
        await asyncio.sleep(config.poll_interval_secs)


async def _ai_review_loop() -> None:
    """Periodically checks whether enough new closed trades have
    accumulated to trigger an AI Free Will review (bot/ai_review.py) --
    cheap no-op most cycles, the real OpenAI call only fires when
    config.ai_review_every_n_trades worth of new trades exist. Checked on
    the same cadence as the screening loop rather than its own faster
    timer -- there's no benefit to checking more often than new trades can
    possibly arrive."""
    while True:
        try:
            await asyncio.to_thread(ai_review.maybe_review_performance)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad review must never kill this loop
            log.exception("AI Free Will review pass crashed -- continuing on the next check.")
        await asyncio.sleep(config.poll_interval_secs)


async def run_forever() -> None:
    """Entry point for the whole engine process -- runs the screening loop,
    bot/exit_manager.py's stop-loss/take-profit loop, AND the AI Free Will
    review loop concurrently in one process (2026-09-06, fully-autonomous
    rebuild). Same relationship Axiom-bot's own main.py has between
    launch-detection, check_exits(), and its own performance_evaluator.py
    calls: one process, several concurrent responsibilities, none blocks
    another. GMGN-bot has no separate main.py -- this IS that process."""
    # .env's TRADING_MODE is now the real, authoritative boot switch
    # (2026-09-06, user's explicit request: a .env switch to flip BEFORE
    # starting the bot, matching Axiom-bot's own MODE=paper/live pattern --
    # "I can go in there and flip it before I start the AI"). Previously
    # config.default_trading_mode was only ever consulted as a fallback
    # when trading_control.json didn't exist yet -- once that file existed
    # from a prior session, whatever mode was left persisted just carried
    # over silently on restart, live or not, with no re-confirmation. This
    # forces the persisted state to match .env on EVERY boot, so a launch
    # always starts in exactly the mode you set in .env, regardless of
    # what was left over from before.
    trading_control.set_mode(config.default_trading_mode)
    log.warning("Trading mode set from .env's TRADING_MODE at startup: %r", config.default_trading_mode)
    import bot.exit_manager as exit_manager
    import bot.dev_snipe as dev_snipe
    import bot.token_snipe as token_snipe
    import bot.ai_judgment as ai_judgment
    # wallet_follow, snipex, skyeye_copy removed from the startup loop
    # 2026-09-16 (user + son's explicit request to cut the "extra weight" --
    # module files themselves archived, not deleted, under archive/; see
    # that folder's own note for how to bring one back).
    #
    # ai_judgment.run_forever() (2026-09-18) -- WATCH AND LOG ONLY, see
    # that module's own docstring. Completely separate from
    # exit_manager.run_forever() above -- never touches a real position,
    # never blocks the real exit-check cycle.
    await asyncio.gather(
        _screen_loop(), exit_manager.run_forever(), _ai_review_loop(),
        dev_snipe.run_forever(), token_snipe.run_forever(), ai_judgment.run_forever(),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        print("\nStopped.")
