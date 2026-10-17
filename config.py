"""Agent configuration -- thresholds and operational settings for the
screening loop. Loaded from environment variables (a project-local .env,
same convention gmgn-cli itself uses) with sane, explicit defaults.

Deliberately mirrors Axiom-bot's own config.py pattern (dataclass + env var
defaults, one field per line with its own comment) -- that shape held up
well across months of real live trading, no reason to reinvent it here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default


def _get_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val in (None, ""):
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _parse_hour_windows(raw: str) -> list[tuple[float, float]]:
    """Parses "4-5,11-13,15-16" into [(4,5), (11,13), (15,16)] -- each pair
    is a [start, end) UTC hour range, wraparound-aware (e.g. "23-2" spans
    11PM to 2AM). Mirrors Axiom-bot's own config.py _parse_hour_windows()
    exactly (2026-09-08, that pattern already proved out across months of
    real live trading there, no reason to reinvent it here). Skips a
    malformed entry rather than raising, so one typo only loses that one
    window, not the whole schedule."""
    windows: list[tuple[float, float]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            start_s, end_s = part.split("-")
            windows.append((float(start_s), float(end_s)))
        except ValueError:
            continue
    return windows


def hour_in_windows(hour: float, windows: list[tuple[float, float]]) -> bool:
    """Same wraparound-aware [start, end) check as Axiom-bot's own
    hour_in_windows() -- centralized here so bot/trading_control.py's
    is_time_paused() has one shared implementation."""
    for start, end in windows:
        if (start <= hour < end) if start <= end else (hour >= start or hour < end):
            return True
    return False


@dataclass
class Config:
    chain: str = field(default_factory=lambda: os.getenv("GMGN_CHAIN", "sol"))

    # -- Screening loop cadence --------------------------------------------
    # How often to poll GMGN's trenches endpoint for new_creation tokens.
    # gmgn-cli itself has no documented rate limit, but polling faster than
    # new real tokens actually appear just wastes calls -- start
    # conservative, tighten once real volume/latency data exists.
    poll_interval_secs: float = field(default_factory=lambda: _get_float("POLL_INTERVAL_SECS", 5.0))

    # -- Rate-limit handling (bot/screener.py) -----------------------------
    # Added 2026-09-06 after a real production incident: removing the
    # bundler_rate cheap-check bug let ~22 candidates/poll through to the
    # real token-security API call (vs. ~0 before), which triggered GMGN's
    # own platform-side rate limit (confirmed live: "HTTP 429
    # code=429 error=RATE_LIMIT_EXCEEDED... repeated requests can extend
    # the ban by 5s up to 5 minutes" -- GMGN's own words).
    token_security_throttle_secs: float = field(
        default_factory=lambda: _get_float("TOKEN_SECURITY_THROTTLE_SECS", 0.75))
    rate_limit_backoff_base_secs: float = field(
        default_factory=lambda: _get_float("RATE_LIMIT_BACKOFF_BASE_SECS", 5.0))
    rate_limit_max_consecutive: int = field(
        default_factory=lambda: _get_int("RATE_LIMIT_MAX_CONSECUTIVE", 3))
    # Default raised 45 -> 300 on 2026-10-17 after the 10/15 incident: GMGN's
    # own 429 text states "repeated requests can extend the ban by 5s up to 5
    # minutes", and with six concurrent loops hammering the same IP a 45s
    # pause was far too short to actually wait out a hard ban. 300s = a full
    # wait out of GMGN's worst-case window before sending another probe.
    # Overridable in .env with RATE_LIMIT_COOLDOWN_SECS.
    rate_limit_cooldown_secs: float = field(
        default_factory=lambda: _get_float("RATE_LIMIT_COOLDOWN_SECS", 300.0))

    # -- Security gate thresholds (fail-closed: missing/None data on any of
    # these is treated as a REJECT, never a silent pass -- same philosophy
    # Axiom-bot's own bot/axiom_monitor.py check_safety() already proved out
    # live) --------------------------------------------------------------
    max_rug_ratio: float = field(default_factory=lambda: _get_float("MAX_RUG_RATIO", 0.3))
    max_top_10_holder_rate: float = field(default_factory=lambda: _get_float("MAX_TOP_10_HOLDER_RATE", 0.3))
    max_bundler_rate: float = field(default_factory=lambda: _get_float("MAX_BUNDLER_RATE", 0.3))
    max_rat_trader_rate: float = field(default_factory=lambda: _get_float("MAX_RAT_TRADER_RATE", 0.2))
    min_liquidity_usd: float = field(default_factory=lambda: _get_float("MIN_LIQUIDITY_USD", 3000.0))
    min_holder_count: int = field(default_factory=lambda: _get_int("MIN_HOLDER_COUNT", 10))
    # Renounced mint/freeze authority -- defaults are required. The paper-
    # mode AI reviewer may tune these switches based on observed outcomes.
    require_renounced_mint: bool = field(default_factory=lambda: _get_bool("REQUIRE_RENOUNCED_MINT", True))
    require_renounced_freeze: bool = field(default_factory=lambda: _get_bool("REQUIRE_RENOUNCED_FREEZE", True))

    # Buy-pressure / momentum entry filter (2026-09-17, user + son's
    # explicit request after real data showed 52 low-liquidity + 22
    # stale-losing exits out of ~99 trades -- tokens clearing entry with
    # zero real upward momentum, just flatlining or bleeding out). Real
    # fields confirmed present on trenches' own response (no extra API
    # call -- verified live, sample of 120 real candidates 2026-09-17):
    # net_buy_24h (buy volume - sell volume, USD), buys_24h (real buy
    # transaction count), is_wash_trading (GMGN's own dedicated flag).
    # NONE of these were checked anywhere before this. Distribution from
    # that same live sample: 8% of candidates had negative net_buy_24h,
    # 12% had buys_24h < 3, 0% had is_wash_trading=True -- these starting
    # thresholds reject a meaningful minority, not the whole pipeline (the
    # $12k liquidity floor mistake earlier today). AI-tunable in paper mode
    # so observed outcomes can shape these two strategy filters. The
    # independent is_wash_trading check below remains fixed and fail-closed
    # on a missing field, same convention as every other check in
    # bot/security.py's check_trenches_fields() -- both fields were 100%
    # populated across the real 120-candidate sample, so this should be
    # rare in practice, not a routine reject path.
    min_net_buy_24h_usd: float = field(default_factory=lambda: _get_float("MIN_NET_BUY_24H_USD", 0.0))
    min_buys_24h: int = field(default_factory=lambda: _get_int("MIN_BUYS_24H", 3))
    # is_wash_trading has no toggle/threshold -- GMGN's own binary
    # manipulation flag, hard-rejected unconditionally when true, same
    # category as the meteora_virtual_curve launchpad exclusion below.

    # -- Staging store ------------------------------------------------------
    cleared_candidates_file: str = field(
        default_factory=lambda: os.getenv("CLEARED_CANDIDATES_FILE", "logs/cleared_candidates.json"))
    # Cross-process lock file (2026-10 latency audit, same reasoning as
    # paper_portfolio_lock_file above) -- _append_trade_log() in
    # bot/screener.py does a read-modify-write on this file, and screening
    # can process more than one cleared candidate in close succession, so
    # the same lost-write race paper_portfolio_lock_file closes applies
    # here too.
    cleared_candidates_lock_file: str = field(
        default_factory=lambda: os.getenv("CLEARED_CANDIDATES_LOCK_FILE", "logs/cleared_candidates.lock"))
    seen_mints_file: str = field(
        default_factory=lambda: os.getenv("SEEN_MINTS_FILE", "logs/seen_mints.json"))
    # Rolling feed of every screening decision (cleared AND rejected), so
    # the dashboard has something to show besides the final cleared list --
    # previously rejections only ever went to the console log, invisible to
    # the web dashboard entirely.
    activity_log_file: str = field(
        default_factory=lambda: os.getenv("ACTIVITY_LOG_FILE", "logs/activity_log.json"))
    # Same cross-process lock reasoning as cleared_candidates_lock_file above.
    activity_log_lock_file: str = field(
        default_factory=lambda: os.getenv("ACTIVITY_LOG_LOCK_FILE", "logs/activity_log.lock"))

    # -- Trading mode ---------------------------------------------------
    # Three modes, persisted in trading_control_file (bot/trading_control.py)
    # so the dashboard can flip it live without a restart:
    #   "off"   -- approve only marks status, nothing executes (default).
    #   "paper" -- approve simulates a fill against a REAL live price
    #              (bot/paper_trader.py) into a virtual portfolio. No
    #              gmgn-cli swap call, no real funds at risk.
    #   "live"  -- real swaps through bot/executor.py. Requires the separate
    #              GMGN_ALLOW_AUTOMATED_TRADES=1 gate and GMGN_PRIVATE_KEY
    #              in gmgn-cli's own local config. Only an operator should
    #              set TRADING_MODE=live and restart; the AI reviewer has
    #              no mode-setting path.
    trading_control_file: str = field(
        default_factory=lambda: os.getenv("TRADING_CONTROL_FILE", "logs/trading_control.json"))
    default_trading_mode: str = field(default_factory=lambda: os.getenv("TRADING_MODE", "off"))

    # Live-session auto-pause (2026-09-06, user's explicit request for a
    # real, code-enforced stop before a live test -- "make sure it pauses
    # for a break after 3 trades", not something relying on manual timing
    # to catch the right moment). Counts LIVE trades closed (any reason --
    # stop-loss/take-profit/profit-giveback/queue-pressure/stale/manual,
    # they all count) since the mode was LAST switched to "live"
    # (trading_control's own updated_ts) -- once that count reaches this
    # many, NEW live buys are blocked (bot/trading_control.py's
    # is_live_session_paused(), checked in bot/screener.py's
    # _autonomous_buy()). Deliberately does NOT change the mode itself --
    # a real bug caught before shipping: bot/executor.py's execute_sell()
    # requires mode == "live" for ANY close, automatic or manual, so
    # switching mode away would strand whatever's still open with no way
    # to sell it. Mode stays "live" throughout; only new entries pause.
    # Checked once per screening cycle, independent of which process
    # actually closed the trade (tui.py's manual hotkeys run in a SEPARATE
    # process from the screener -- this reads the shared
    # trade_history.json file, so it catches manual closes too).
    # AI-tunable in paper mode; any override remains in effect if the
    # operator later switches to live.
    max_live_trades_per_session: int = field(
        default_factory=lambda: _get_int("MAX_LIVE_TRADES_PER_SESSION", 3))

    # Rolling trade-burst cooldown (2026-09-07, user's explicit request ahead
    # of funding the wallet with real money: "cool down after 10 trades...
    # have the pause last 2 minutes"). Deliberately separate from
    # max_live_trades_per_session above -- that one is a hard, LIVE-ONLY,
    # session-long stop that never auto-clears on its own (only a mode
    # switch clears it). This one is mode-agnostic (counts paper AND live
    # buys, so it's exercised and verifiable in paper mode before real funds
    # are on the line), rolling (resets its own count once tripped), and
    # auto-resumes on its own after trade_burst_cooldown_secs -- a genuine
    # cooldown, not a stop. Same "don't strand open positions" reasoning as
    # max_live_trades_per_session: only blocks NEW buys, exit management
    # (stop-loss/take-profit/etc.) keeps running normally on whatever's
    # already open. State lives in trade_burst_state_file below, tracked by
    # bot/trading_control.py's record_trade_for_burst()/is_burst_paused(),
    # checked in bot/screener.py's _autonomous_buy() right alongside the
    # other pre-buy gates.
    trade_burst_limit: int = field(
        default_factory=lambda: _get_int("TRADE_BURST_LIMIT", 10))
    trade_burst_cooldown_secs: float = field(
        default_factory=lambda: _get_float("TRADE_BURST_COOLDOWN_SECS", 120.0))
    trade_burst_state_file: str = field(
        default_factory=lambda: os.getenv("TRADE_BURST_STATE_FILE", "logs/trade_burst_state.json"))
    # Cross-process lock (2026-09-10, found during a full-codebase audit):
    # this file's read-modify-write in bot/trading_control.py's
    # record_trade_for_burst()/record_trade_time() never got the same
    # file_lock.cross_process_lock() treatment paper_portfolio_lock_file/
    # live_positions_lock_file/ai_memory_lock_file/ai_overrides_lock_file
    # above all already have, despite being the exact same kind of shared
    # state (written by bot/screener.py's engine process, read by tui.py's
    # separate process for its header cooldown badges). No confirmed live
    # race yet -- only the engine process writes to it today -- but every
    # other shared-state file in this project gets this protection
    # unconditionally rather than waiting for a real incident to prove it's
    # needed, and this one shouldn't be the one exception.
    trade_burst_state_lock_file: str = field(
        default_factory=lambda: os.getenv("TRADE_BURST_STATE_LOCK_FILE", "logs/trade_burst_state.lock"))

    # Minimum time between trades (2026-09-09, user's explicit request: real
    # fee drag found live -- wallet dropped ~0.169 SOL in one session while
    # closed-trade PnL only summed to about -0.02 SOL, the other ~0.15 SOL
    # being round-trip fees (~11-12% per trade, see the same-night fee
    # measurement) across 14 rapid round trips. This is a separate, dedicated
    # throttle: a flat minimum number of seconds between one buy and the
    # next, mode-agnostic like trade_burst_limit above, state in the same
    # trade_burst_state_file (a "last_trade_ts" field alongside the burst
    # counter). AI-tunable in paper mode along with other trade-frequency
    # strategy dials. 0 disables.
    min_time_between_trades_secs: float = field(
        default_factory=lambda: _get_float("MIN_TIME_BETWEEN_TRADES_SECS", 10.0))

    # -- Time-of-day pause (2026-09-08, user's explicit request after we
    # built a real hour-by-hour schedule from 1,382 real trades spanning
    # nearly a full 24-hour cycle -- see SCHEDULE.md for the full table and
    # methodology, including which "good" hours turned out to be single-
    # trade illusions once checked). Mirrors Axiom-bot's own
    # TIME_PAUSE_ENABLED/TIME_PAUSE_WINDOWS_UTC exactly (same
    # _parse_hour_windows/hour_in_windows helpers above). Windows are UTC;
    # the confirmed-bad LOCAL (Mountain, UTC-7) hours this was built from
    # are 21-22 (9-10PM), 8-9AM, 4-6AM, 12-1PM, and 2-3PM -- each +7h to get
    # the UTC values below. This reverses an earlier explicit decision
    # (2026-09-07: "the gentleman really doesn't need a trade time
    # schedule... I want it like that") -- flagged, not silently changed;
    # the user made this call deliberately with much more data in hand.
    # Only blocks NEW buys -- exit management keeps running normally on
    # anything already open when a pause window starts, same reasoning as
    # every other pause mechanism in this project.
    time_pause_enabled: bool = field(default_factory=lambda: _get_bool("TIME_PAUSE_ENABLED", True))
    # Both the enable switch and UTC windows are AI-tunable in paper mode.
    time_pause_windows_utc: list[tuple[float, float]] = field(
        default_factory=lambda: _parse_hour_windows(
            os.getenv("TIME_PAUSE_WINDOWS_UTC", "4-5,11-13,15-16,19-20,21-22")))

    # -- Paper trading ----------------------------------------------------
    paper_portfolio_file: str = field(
        default_factory=lambda: os.getenv("PAPER_PORTFOLIO_FILE", "logs/paper_portfolio.json"))
    # Cross-process lock file (2026-09-09, see bot/file_lock.py's own
    # docstring for the real bug this closes -- tui.py and bot/screener.py
    # are separate OS processes that both read-modify-write this same
    # portfolio file). Deliberately its own file, not the data file itself
    # -- locking the data file directly would conflict with its own atomic
    # tempfile-then-os.replace write pattern.
    paper_portfolio_lock_file: str = field(
        default_factory=lambda: os.getenv("PAPER_PORTFOLIO_LOCK_FILE", "logs/paper_portfolio.lock"))
    # Simulated SOL size per paper buy -- same field name/default Axiom-bot's
    # own live config uses for its real trade size, so the number means the
    # same thing across both projects when comparing results later.
    trade_size_sol: float = field(default_factory=lambda: _get_float("TRADE_SIZE_SOL", 0.05))
    # Simulated round-trip fee for PAPER trades only (2026-09-09, user's
    # explicit request: "add those fees so they can reflect here on
    # screen"). A LIVE close's exit_price comes from an actual executed
    # GMGN swap, so the real fee is already baked into that price -- but
    # paper mode's price lookup is a plain quote with no swap and no fee
    # behind it, so its raw price-difference PnL has always been
    # fee-blind. 0.115 (11.5%) is the midpoint of the ~11-12% round-trip
    # fee measured from real manual test trades earlier this session (see
    # bot/ai_review.py's own documented figure) -- used here as-measured
    # rather than reconstructed from GMGN's published fee schedule, since
    # that schedule (1% GMGN + gas + platform fee) is only ~2-3% and the
    # rest of the real-world drag is slippage on thin pump.fun pools,
    # which isn't separately modeled. 0 disables (reverts to fee-blind).
    paper_simulated_fee_pct: float = field(
        default_factory=lambda: _get_float("PAPER_SIMULATED_FEE_PCT", 0.115))

    # -- Live execution (bot/executor.py) --------------------------------
    # Public wallet address bound to the GMGN API key -- required by
    # `swap --from <address>`. Public address only; the matching private
    # key is never read, stored, or handled by this project -- gmgn-cli
    # reads GMGN_PRIVATE_KEY from its own local config directly.
    wallet_address: str = field(default_factory=lambda: os.getenv("GMGN_WALLET_ADDRESS", ""))
    slippage_pct: float = field(default_factory=lambda: _get_float("SLIPPAGE_PCT", 30.0))
    priority_fee_sol: float = field(default_factory=lambda: _get_float("PRIORITY_FEE_SOL", 0.0002))
    live_positions_file: str = field(
        default_factory=lambda: os.getenv("LIVE_POSITIONS_FILE", "logs/live_positions.json"))
    # Same cross-process lock reasoning as paper_portfolio_lock_file above.
    live_positions_lock_file: str = field(
        default_factory=lambda: os.getenv("LIVE_POSITIONS_LOCK_FILE", "logs/live_positions.lock"))

    # -- Exit management (bot/exit_manager.py) ---------------------------
    # Autonomous stop-loss/take-profit -- the one safety net kept in place
    # per the user's explicit 2026-09-06 direction ("no guardrails" on
    # entries, but exit protection stays). These starting values are NOT
    # backtested against real GMGN/pump.fun-style token behavior the way
    # Axiom-bot's own risk parameters were refined over many live sessions
    # -- they're a conservative starting point, flagged here so a future
    # session doesn't mistake them for validated numbers.
    stop_loss_pct: float = field(default_factory=lambda: _get_float("STOP_LOSS_PCT", 20.0))
    take_profit_pct: float = field(default_factory=lambda: _get_float("TAKE_PROFIT_PCT", 50.0))
    # Profit-giveback exit (2026-09-06, user's explicit request: "he does
    # not have to wait for the 50% TP to sell if he sees the trade
    # starting to revert and he has profit"). A position that peaked at
    # some pnl_pct and has since given back this many percentage-points
    # from that peak -- while STILL net positive -- closes early instead
    # of riding it back down toward (or past) breakeven waiting for
    # take_profit_pct. Deliberately percentage-POINTS off the peak, not a
    # percentage OF the peak -- simpler to reason about and to explain in
    # a lesson (bot/ai_review.py can tune this the same way it tunes
    # stop_loss_pct/take_profit_pct). NOT backtested, same caveat as the
    # stop-loss/take-profit defaults above.
    profit_giveback_pct: float = field(default_factory=lambda: _get_float("PROFIT_GIVEBACK_PCT", 15.0))

    # Queue-pressure exit (2026-09-06, user's own observation: with only
    # max_concurrent_positions slots and candidates piling up behind that
    # cap -- recorded as "skipped_max_positions" in cleared_candidates.json
    # -- holding a position out for the full take_profit_pct has a real
    # opportunity cost: a occupied slot can't take a fresh, possibly-better
    # cleared candidate. If a backlog of skipped candidates exists AND a
    # position already has at least this much profit (well below
    # take_profit_pct is fine), it closes early with reason
    # "queue-pressure" to free the slot, rather than holding out for the
    # full target with nothing else able to trade meanwhile. Independent
    # of profit_giveback_pct -- this can fire even on a position that's
    # still climbing, not just one reverting from a peak.
    queue_pressure_min_profit_pct: float = field(
        default_factory=lambda: _get_float("QUEUE_PRESSURE_MIN_PROFIT_PCT", 20.0))
    # How far back to look in cleared_candidates.json for a
    # "skipped_max_positions" entry to count as CURRENT backlog pressure --
    # an old skip from an hour ago doesn't mean anything is waiting right
    # now. AI-tunable in paper mode.
    queue_pressure_lookback_secs: float = field(
        default_factory=lambda: _get_float("QUEUE_PRESSURE_LOOKBACK_SECS", 60.0))

    # Stale-position exit (2026-09-06, user's explicit request: "he needs
    # to drop these tokens that are not going anywhere ... learn to drop
    # like Mmusk" -- a real observed position sitting near-flat/slightly
    # negative for minutes, occupying one of only max_concurrent_positions
    # scarce slots with nothing to show for it). Pure time-based cutoff:
    # if a position has been open this long and none of the other exit
    # rules (stop-loss/take-profit/profit-giveback/queue-pressure) have
    # fired, close it anyway with reason "stale" -- it doesn't need to be
    # LOSING money, just not proving itself, to get cut loose. Checked
    # LAST (lowest priority) in bot/exit_manager.py's decision order --
    # every other rule gets first say if it applies.
    stale_position_max_age_secs: float = field(
        default_factory=lambda: _get_float("STALE_POSITION_MAX_AGE_SECS", 180.0))

    # Flat-coin grace period (2026-09-10, built after a real finding: 201
    # "stale" exits averaged +1.78% RAW price move (mostly right on
    # direction) but -9.72% after the ~11.5% round-trip fee -- the fee
    # alone was erasing a mostly-correct call, not the exit timing or
    # entry quality. The AI had been asking to shrink
    # stale_position_max_age_secs in response, which would make this
    # WORSE (more small-fee-eaten trades per hour, not fewer). This is the
    # opposite fix: a position sitting near-flat (neither a real winner
    # nor a real loser) gets EXTRA time beyond the normal stale cutoff to
    # actually develop into a move big enough to clear the fee hurdle,
    # instead of being cut on the same clock as everything else. A
    # position that ISN'T flat (already trending some real direction) is
    # untouched -- it still exits on stop-loss/take-profit/
    # profit-giveback/queue-pressure/the normal stale clock exactly as
    # before. See bot/exit_manager.py's _decide_exit_reason() for the
    # actual grace-period logic.
    #
    # flat_pnl_band_pct: a position whose CURRENT pnl_pct is within +/-
    # this many percentage points of 0 counts as "flat" at the moment the
    # normal stale_position_max_age_secs clock would otherwise fire.
    flat_pnl_band_pct: float = field(default_factory=lambda: _get_float("FLAT_PNL_BAND_PCT", 3.0))
    # flat_position_extra_secs: how much ADDITIONAL time (beyond
    # stale_position_max_age_secs) a flat position gets before it closes
    # regardless -- bounded, not unlimited grace. If it's still flat once
    # this extra window also elapses, it closes with reason "stale" same
    # as always (this doesn't add a new exit reason, just conditionally
    # extends the existing clock).
    flat_position_extra_secs: float = field(default_factory=lambda: _get_float("FLAT_POSITION_EXTRA_SECS", 120.0))

    # losing_position_max_age_secs (2026-09-17, updated 2026-10-12 per
    # user's standing two-minute rule): a position that is NOT currently
    # in profit (pnl_pct <= 0) gets cut after 120 seconds instead of waiting
    # for the full stale_position_max_age_secs. Profitable positions skip
    # this cutoff and continue under their normal exit rules. Deliberately
    # a SEPARATE field from stale_position_max_age_secs, not a change to it.
    # AI-tunable in paper mode as part of the losing-position exit strategy.
    # Checked in bot/exit_manager.py's _decide_exit_reason() ahead of the
    # normal stale/flat-grace check.
    losing_position_max_age_secs: float = field(
        default_factory=lambda: _get_float("LOSING_POSITION_MAX_AGE_SECS", 120.0))

    # stop_loss_confirm_secs (2026-09-17, user + son's explicit request to
    # prevent early whipsaws during choppy price action): the stop-loss
    # check in bot/exit_manager.py's _decide_exit_reason() now requires the
    # pnl_pct <= -stop_loss_pct condition to hold continuously for this many
    # seconds (tracked in-process, per address) before actually closing the
    # position, instead of firing on whatever a single 5s-interval price
    # tick happened to read. A position that dips through the stop-loss
    # line and recovers before this window elapses is left open -- exactly
    # the "early whipsaw" scenario this responds to. 0 disables the guard
    # entirely (fires instantly on the very first breaching tick, the
    # previous behavior), same disable convention every other threshold in
    # this project uses. AI-tunable in paper mode as an exit-strategy dial.
    stop_loss_confirm_secs: float = field(
        default_factory=lambda: _get_float("STOP_LOSS_CONFIRM_SECS", 10.0))

    # emergency_drawdown_pct (2026-09-17, user + son's real-time catch: two
    # separate confirmed incidents THE SAME SESSION -- RIM exited
    # "stop-loss" at 18.4s already at -15.4% raw, OMG exited "stop-loss" at
    # 12.6s already at -27.1% raw, both massively overshooting the 5%
    # stop_loss_pct line because stop_loss_confirm_secs's own 10-second
    # whipsaw-confirmation window was still counting down while the price
    # kept falling. The guard is doing exactly its job against noise; this
    # is the escape hatch for when it ISN'T noise -- a position already
    # this far past its stop-loss line skips the confirmation wait entirely
    # and exits immediately, same as every exit did before the whipsaw
    # guard existed. Uses the same RAW pnl_pct basis stop_loss_pct itself
    # already compares against (bot/exit_manager.py's _pnl_pct(), no fee
    # applied) for direct consistency. Default chosen from the real
    # incidents above -- 15.0 would have caught RIM right as it crossed
    # this line instead of letting it ride to -15.4%, and OMG even sooner.
    # 0 disables (falls back to the full confirm-window behavior for every
    # breach, no severity exception). AI-tunable in paper mode.
    emergency_drawdown_pct: float = field(
        default_factory=lambda: _get_float("EMERGENCY_DRAWDOWN_PCT", 15.0))

    # max_hold_secs (2026-09-18, user's explicit request, modeled on
    # Axiom-bot's own momentum-bucket 3s max-hold): a genuinely UNCONDITIONAL
    # ceiling -- closes the position the instant age_secs reaches this,
    # regardless of current PnL, win or loss. Different from every other
    # time-based exit above: stale_position_max_age_secs/
    # losing_position_max_age_secs only fire if nothing else already did
    # (and the losing one only applies while pnl_pct <= 0); this one is
    # checked FIRST (right after the liquidity-death check, ahead of
    # stop-loss/emergency-drawdown/take-profit/everything), so it can cut
    # off a position that's STILL WINNING at the deadline, not just a loser.
    # Built after real evidence 2026-09-17/18: 11 emergency-drawdown exits
    # in one session, entry market caps all under $6000, crashing in as
    # little as 2.4s -- the goal is catching some of these even earlier
    # than the -15% emergency line does. Real, known tradeoff: this SAME
    # session's take-profit wins averaged an 11.5s hold before resolving --
    # a short max_hold_secs would force those out early too, at whatever
    # price they happen to be at that exact moment, not their eventual
    # take-profit price. Granularity bounded by exit_check_interval_secs
    # (currently 2.0s) -- this can't fire more precisely than that poll
    # cycle allows. 0 disables entirely. AI-tunable in paper mode.
    max_hold_secs: float = field(
        default_factory=lambda: _get_float("MAX_HOLD_SECS", 5.0))

    # min_exit_liquidity_usd (2026-09-14): a position closes IMMEDIATELY,
    # regardless of current PnL, the moment its own pool liquidity (USD --
    # verified live against a real token, same convention min_liquidity_usd
    # above uses -- same get_token_info() field bot/exit_manager.py already
    # fetches every check cycle) drops below this. The paper-mode reviewer
    # may tune this after the nine-position liquidity-loss incident it
    # responds to.
    min_exit_liquidity_usd: float = field(default_factory=lambda: _get_float("MIN_EXIT_LIQUIDITY_USD", 200.0))

    exit_check_interval_secs: float = field(default_factory=lambda: _get_float("EXIT_CHECK_INTERVAL_SECS", 5.0))
    # Added 2026-09-06, real bug found live: with 60 open paper positions
    # checked one at a time (~0.47s each), a full exit-check pass took ~28s
    # -- meaning the last position checked each cycle used price data up to
    # 28s stale, not the ~5s the interval implies. Several positions blew
    # through stop_loss_pct to -74%/-100% before ever getting checked in
    # time. Batched concurrency fetches this many positions' prices AT ONCE
    # per batch instead of one at a time -- cuts a 60-position pass to
    # roughly (60/batch_size) x one-price-fetch-time instead of 60x it.
    # Not unlimited/all-at-once on purpose: bursting every open position's
    # price fetch simultaneously risks a harsher GMGN rate-limit response
    # than the current spread-out pattern, especially given tonight's
    # already-active rate limiting.
    exit_check_batch_size: int = field(default_factory=lambda: _get_int("EXIT_CHECK_BATCH_SIZE", 10))

    # Max concurrent open positions (2026-09-06, user's explicit request:
    # "only execute 3 trades at one single time... that way he doesn't
    # burn through the wallet that fast"). This is the ONE entry-side
    # limit reintroduced after the earlier "no guardrails on entries"
    # direction -- a pure capital-exposure cap, not a risk/quality filter
    # (bot/security.py's screening is untouched). AI-tunable in paper mode;
    # checked against open positions in the active mode.
    max_concurrent_positions: int = field(default_factory=lambda: _get_int("MAX_CONCURRENT_POSITIONS", 3))

    # Market cap trading band (2026-09-16, user's explicit, real-data-backed
    # request: "he trade tokens between 20,000 and 100,000... nothing higher
    # then 100,000"). Built after checking real recent trade data at the
    # user's own request -- every single trade above $1M market cap in the
    # prior hour LOST money (0% win rate, 6 for 6), while the vast majority
    # of trades in the $5.8K-$76K range were normal, expected fresh-deploy
    # activity. A mature, already-established token (the kind that ends up
    # at $70M-$200M) doesn't move fast enough for this strategy to catch
    # anything, but still costs real fee/slippage drag every time it's
    # bought -- a small, consistent, avoidable loss.
    #
    # AI-tunable in paper mode. Applied in BOTH
    # bot/security.py's check_trenches_fields()
    # (original screening) and check_arbitrary_token_candidate() (the shared
    # check wallet_follow/dev_snipe/skyeye_copy/token_snipe/snipex all use)
    # so every signal source is held to the exact same boundary -- no
    # separate/looser standard for any one source. <= 0 disables either
    # bound, same convention every other disable-able threshold here uses.
    min_market_cap_usd: float = field(default_factory=lambda: _get_float("MIN_MARKET_CAP_USD", 20000.0))
    max_market_cap_usd: float = field(default_factory=lambda: _get_float("MAX_MARKET_CAP_USD", 100000.0))

    # min_liquidity_to_mcap_ratio (2026-09-17, user's son's real-time
    # observation while trading manually: a lot of top-heavy tokens where
    # market cap is artificially inflated relative to actual pool depth --
    # a fake pump that looks big on market_cap alone but has almost nothing
    # real backing it, liable to dump the instant anyone tries to sell into
    # it). liquidity/market_cap below this ratio is rejected -- e.g. 0.05
    # means the pool must hold at least 5% of the token's own market cap in
    # real liquidity. Different signal than min_liquidity_usd/
    # min_market_cap_usd above: those are independent absolute floors, this
    # one catches a token that could pass BOTH absolute floors individually
    # while still being wildly lopsided relative to each other (e.g. $15k
    # liquidity against a $500k market cap passes both absolute checks but
    # is a 3% ratio, exactly the "top-heavy" pattern this targets). Same
    # category as min/max_market_cap_usd -- AI-tunable in paper mode, applied
    # in BOTH check_trenches_fields() and
    # check_arbitrary_token_candidate() so every signal source is held to
    # the same boundary. <= 0 disables it, same convention as every other
    # disable-able threshold here.
    min_liquidity_to_mcap_ratio: float = field(
        default_factory=lambda: _get_float("MIN_LIQUIDITY_TO_MCAP_RATIO", 0.05))

    # new_creation_max_age (2026-09-18, user's explicit request): passed
    # straight through to gmgn-cli's own `--max-created` trenches filter
    # (bot/screener.py's screen_once(), new_creation category only --
    # verified live this is a request-wide filter, not per-category, so it
    # cannot also apply to the "completed" category without killing it
    # entirely, migrated tokens are inherently far older). Real evidence
    # this responds to: unfiltered, new_creation candidates were showing up
    # with a median age of ~100+ seconds by the time screening ever saw
    # them (GMGN's own indexing lag, confirmed NOT caused by our own
    # poll_interval_secs -- that's only 5s, nowhere near enough to explain
    # a 100+ second gap). User wants him catching genuine newborns instead.
    # String, not a float -- gmgn-cli's own duration format (e.g. "30s",
    # "5m"), passed through verbatim, no parsing done on this side. Empty
    # string disables the filter entirely (falls back to GMGN's own
    # default, unfiltered ordering). AI-tunable in paper mode.
    new_creation_max_age: str = field(
        default_factory=lambda: os.getenv("NEW_CREATION_MAX_AGE", "30s"))

    # Liquidity-drain exit signal (2026-09-19, user's explicit direction
    # after watching live trending charts: "we need to trade regardless of
    # the amount of holders snipers bundlers, they tend to hold on for a
    # while once the coin has good market cap, just needs to be vigilant of
    # the liquidity once it's over 18,000 market cap because if that starts
    # to drop quick that's his cue to sell"). Two-part rule in
    # bot/exit_manager.py's _decide_exit_reason(): only ARMS once a
    # position's current market_cap has reached liquidity_drain_mcap_threshold_usd
    # (below that, the existing low-liquidity floor / normal exit rules
    # already apply), then compares current liquidity against the highest
    # liquidity this position has ever shown (peak_liquidity, tracked the
    # same way peak_pnl_pct already is) -- a drop of liquidity_drain_pct or
    # more from that peak is the sell signal, independent of whether pnl is
    # currently positive or negative. Real, deliberate replacement for
    # relying on holder-concentration/sniper/bundler metrics as the risk
    # signal at this stage -- user's own observation is that those tend to
    # hold their position once market cap is healthy, so liquidity itself
    # draining is the more honest tell. AI-tunable in paper mode.
    # <= 0 on either disables the
    # whole rule, same convention as every other disable-able threshold
    # here. 30% chosen as a starting default for "drop quick" -- no real
    # trade data yet at the time this was built to calibrate a precise
    # number, revisit once real trending-era liquidity-drain trades exist.
    liquidity_drain_mcap_threshold_usd: float = field(
        default_factory=lambda: _get_float("LIQUIDITY_DRAIN_MCAP_THRESHOLD_USD", 18000.0))
    liquidity_drain_pct: float = field(
        default_factory=lambda: _get_float("LIQUIDITY_DRAIN_PCT", 30.0))

    # Post-review pause (2026-09-06, user's explicit request: give the AI
    # "time to review his data and see where the mistakes were"). After
    # bot/ai_review.py actually runs a real review (not a skipped no-op),
    # new buys pause for this many seconds -- screening/rejection and exit
    # management (stop-loss/take-profit/profit-giveback on positions
    # already open) both keep running unaffected, only NEW entries pause.
    # Gives a just-adjusted parameter a clean gap before more trades pile
    # up under it, rather than the review running mid-buy-stream with no
    # visible effect.
    # AI-tunable in paper mode; 0 disables the pause.
    post_review_pause_secs: float = field(default_factory=lambda: _get_float("POST_REVIEW_PAUSE_SECS", 120.0))

    # -- Trade history (bot/trade_history.py) ----------------------------
    # Unified log every closed trade writes to (paper AND live, regardless
    # of exit reason -- stop-loss/take-profit/manual) -- built 2026-09-06
    # specifically so the future AI Free Will review loop has one real,
    # structured source of trade outcomes to learn from, instead of the
    # two separate paper/live "closed" lists each trader already keeps for
    # its own portfolio bookkeeping.
    trade_history_file: str = field(
        default_factory=lambda: os.getenv("TRADE_HISTORY_FILE", "logs/trade_history.json"))

    # Persistent, NEVER-cleared trade log (2026-09-11, user's explicit
    # request: "build him actual access to his full trade history... make
    # sure all trades are saved even on a dash refresh"). Real gap found
    # live: the "clear the dash" ritual (archives trade_history_file to
    # logs/archive/*_manual.json, resets the live file to []) has been run
    # several times a session -- each time, the AI Free Will review loop's
    # own trade_count (read from trade_history_file) dropped back near 0
    # while memory["last_reviewed_trade_count"] in ai_memory.json (NOT
    # touched by a dash clear) stayed at whatever it was before. That gate
    # (`trade_count - last_reviewed_trade_count < ai_review_every_n_trades`)
    # would have silently gone deeply negative and stalled reviews for
    # potentially hundreds of trades after every single dash clear -- it
    # was only ever rescued because an unanswered user_note ALSO bypasses
    # that gate, and one has usually been sitting there. Without a pending
    # note, this would have been a real, silent stall.
    #
    # trade_history_master_file is written to by record_trade() ALONGSIDE
    # trade_history_file (same call, both get the entry) but is never
    # touched by a dash clear -- bot/ai_review.py reads FROM THIS FILE now
    # for its own trade_count/stats, so his review cadence and his
    # learning history are both immune to the dashboard being reset.
    # tui.py's live display still reads trade_history_file, which still
    # gets cleared on request -- the dashboard view and the AI's own
    # memory are now deliberately decoupled.
    trade_history_master_file: str = field(
        default_factory=lambda: os.getenv("TRADE_HISTORY_MASTER_FILE", "logs/trade_history_master.json"))

    # Cross-process locks (2026-09-12, same real bug class as
    # paper_portfolio_lock_file/live_positions_lock_file/ai_memory_lock_file
    # above -- trade_history.py's record_trade() was the one shared-state
    # writer in this whole project that never got this treatment. Found by
    # cross-checking paper_portfolio.json's own closed-trade list against
    # trade_history_master.json: a real ~16h45m stretch (9/11 2:50pm -
    # 9/12 7:36am) where 180 real trades never made it into the master log
    # at all, plus 906 orphaned .tmp files piling up in logs/ (10 on 9/9,
    # growing to 507 on 9/12 alone) from failed atomic swaps -- the
    # unlocked read-modify-write in _append_bounded() losing races against
    # whatever else was touching the same file at the same instant. Two
    # separate lock files (not one shared with any other data file) --
    # locking the data file directly would conflict with its own atomic
    # tempfile-then-os.replace write pattern, same reasoning as every other
    # lock file in this project.
    trade_history_lock_file: str = field(
        default_factory=lambda: os.getenv("TRADE_HISTORY_LOCK_FILE", "logs/trade_history.lock"))
    trade_history_master_lock_file: str = field(
        default_factory=lambda: os.getenv("TRADE_HISTORY_MASTER_LOCK_FILE", "logs/trade_history_master.lock"))

    # -- Wallet-follow signal source (bot/wallet_follow.py) --------------
    # Built 2026-09-15, first piece of the CopyTrade toolkit integration.
    # NOT a separate trader with its own rules -- a signal source only,
    # feeding candidates through bot/screener.py's existing _autonomous_buy()
    # exactly like trenches screening does, same risk gates, same exit
    # management, same everything. See bot/wallet_follow.py's own module
    # docstring for the full architecture reasoning (user's explicit call:
    # "I really hate separating logics in to different buckets").
    #
    # Pilot wallet is Cented (real trader, personal relationship with the
    # user -- "I chat with him on and off"). GMGN's own platform tags him
    # "wash_trader" alongside "kol"/"arbitrager"/"top_followed" -- user's
    # explicit, deliberate call, knowingly overridden: "I don't care what
    # gmgn has on cented I want to use cented." Do not second-guess this
    # choice in code or comments elsewhere -- it's settled.
    wallet_follow_address: str = field(
        default_factory=lambda: os.getenv("WALLET_FOLLOW_ADDRESS", ""))
    # Must be tracked via GMGN's own platform "Track" button FIRST -- the
    # `--wallet` filter on `gmgn-cli track follow-wallet` only returns
    # trades for wallets already on THIS account's own follow list (real
    # gmgn-cli documentation, confirmed live 2026-09-14/15: the CLI looked
    # completely broken -- returning an unrelated trending wallet's trades
    # regardless of which address was requested, even a nonexistent one --
    # until the target wallet was actually Tracked on the platform, at
    # which point its real trades started coming through correctly).
    wallet_follow_enabled: bool = field(default_factory=lambda: _get_bool("WALLET_FOLLOW_ENABLED", False))
    # 10-15s is plenty -- Cented isn't reacting on millisecond timing, and
    # this shares the same overall gmgn-cli rate-limit budget as screening/
    # exit-checks. Documented limiter for this specific endpoint (gmgn-cli's
    # own skill docs): weight 3 against a 20-capacity/20-rate leaky bucket,
    # so even much faster polling would stay within budget -- this is a
    # deliberate choice to not push it, not a limit we're up against.
    wallet_follow_poll_interval_secs: float = field(
        default_factory=lambda: _get_float("WALLET_FOLLOW_POLL_INTERVAL_SECS", 12.0))
    wallet_follow_state_file: str = field(
        default_factory=lambda: os.getenv("WALLET_FOLLOW_STATE_FILE", "logs/wallet_follow_state.json"))
    wallet_follow_state_lock_file: str = field(
        default_factory=lambda: os.getenv("WALLET_FOLLOW_STATE_LOCK_FILE", "logs/wallet_follow_state.lock"))

    # -- Token Snipe signal source (bot/token_snipe.py) -------------------
    # Built 2026-09-15, second piece of the CopyTrade toolkit integration,
    # same day as wallet-follow. Same "signal source only" architecture --
    # see bot/wallet_follow.py's own module docstring for the full
    # reasoning, applies here too. User feeds a specific token address
    # directly in chat (NOT through the AI's own note-reading -- exact
    # contract addresses are precise data entry, not a strategy judgment
    # call, same reasoning that already ruled out free-text parsing for
    # Buzz a while back) -- Claude adds it straight to the watchlist file.
    # The watch loop then waits for that specific token to have REAL
    # liquidity (checks the meaning of "has liquidity" only -- the actual
    # security.check_arbitrary_token_candidate() threshold check happens
    # separately once liquidity appears, same shared check wallet-follow
    # uses). Covers BOTH a genuinely pre-launch token (waits) and an
    # already-migrated one with real volume (fires almost immediately,
    # since it already qualifies) -- user's own clarification: "he can
    # still do it even if it already migrated to that kind of volume."
    token_snipe_enabled: bool = field(default_factory=lambda: _get_bool("TOKEN_SNIPE_ENABLED", False))
    token_snipe_poll_interval_secs: float = field(
        default_factory=lambda: _get_float("TOKEN_SNIPE_POLL_INTERVAL_SECS", 10.0))
    token_snipe_watchlist_file: str = field(
        default_factory=lambda: os.getenv("TOKEN_SNIPE_WATCHLIST_FILE", "logs/token_snipe_watchlist.json"))
    token_snipe_watchlist_lock_file: str = field(
        default_factory=lambda: os.getenv("TOKEN_SNIPE_WATCHLIST_LOCK_FILE", "logs/token_snipe_watchlist.lock"))
    # Tracks per-address status (watching/bought) so a token already bought
    # is never re-bought on a later poll, and a token that never qualifies
    # just keeps getting cheaply re-checked rather than needing any manual
    # cleanup -- no separate "permanently rejected" classification, since
    # guessing which rejection reasons are truly permanent vs. could change
    # (liquidity can still arrive later) isn't worth the complexity unless
    # real behavior shows it's needed.
    token_snipe_state_file: str = field(
        default_factory=lambda: os.getenv("TOKEN_SNIPE_STATE_FILE", "logs/token_snipe_state.json"))
    token_snipe_state_lock_file: str = field(
        default_factory=lambda: os.getenv("TOKEN_SNIPE_STATE_LOCK_FILE", "logs/token_snipe_state.lock"))

    # -- Dev Snipe signal source (bot/dev_snipe.py) -----------------------
    # Built 2026-09-15, third piece of the CopyTrade toolkit -- built AHEAD
    # of finishing Token Snipe, at the user's explicit request, after a real
    # question mid-Token-Snipe-build: "what if the token developer launches
    # different tokens not just this one is he going to snipe the other
    # ones also" -- that's this feature, not Token Snipe's (which only ever
    # watches the specific token addresses it's given). Same "signal source
    # only" architecture as wallet-follow/token-snipe -- see
    # bot/wallet_follow.py's own module docstring for the full reasoning.
    #
    # Watches a DEV WALLET (not a token) for every future token they deploy,
    # buying each fresh launch and mirroring the exit when the dev sells
    # their own creation. Same watchlist-file pattern as Token Snipe -- user
    # feeds dev wallet addresses directly in chat, not through free-text
    # parsing (same precedent). Multiple devs supported from day one (a
    # JSON array in the watchlist file), unlike wallet-follow's single
    # hardcoded pilot address, since the user is expected to want more than
    # one dev watched over time.
    #
    # Mechanism verified live 2026-09-15 via two real, never-Tracked dev
    # wallets (gmgn-cli's own installed source + skills/gmgn-portfolio/
    # SKILL.md, not guessed): UNLIKE `track follow-wallet`, this does NOT
    # need the target wallet Tracked via GMGN's platform UI first --
    # `portfolio created-tokens`/`portfolio activity` both use lighter
    # "exist auth" (API key only), not the signed/account-scoped "critical
    # auth" that caused wallet-follow's Track-first requirement. So any dev
    # address can be added to the watchlist and watched immediately, no
    # manual platform step needed.
    #   Buy signal: `portfolio created-tokens` polled per dev, a fresh
    #     create_timestamp beyond the last-seen baseline = a brand-new
    #     deployment -> runs through the same
    #     security.check_arbitrary_token_candidate() wallet-follow uses.
    #     Confirmed live: the tokens array returns newest-first, so even if
    #     a prolific dev's history is truncated at the documented 100-entry
    #     cap, it's always the OLDEST entries that fall off, never the
    #     newest -- a fresh launch can never be silently missed this way.
    #   Sell signal: `portfolio activity --type sell` polled per dev,
    #     mirrors the exit for ANY sell of a token we hold a dev_snipe-
    #     sourced position in. Deliberately does NOT try to read
    #     `is_open_or_close` (full vs partial close) for this endpoint --
    #     unlike follow-wallet, gmgn-cli's own docs do not document that
    #     field's meaning for `portfolio activity` specifically, so this
    #     treats every sell as a real signal rather than guess at an
    #     undocumented distinction.
    dev_snipe_enabled: bool = field(default_factory=lambda: _get_bool("DEV_SNIPE_ENABLED", False))
    # Heavier per-cycle cost than wallet-follow (two endpoints per watched
    # dev: created-tokens weight 2 + activity weight 3, both against the
    # same shared 20/20 leaky bucket other gmgn-cli calls also draw from),
    # and meant to scale to multiple watched devs -- 15s is a deliberately
    # slower default than wallet-follow's 12s for that reason.
    dev_snipe_poll_interval_secs: float = field(
        default_factory=lambda: _get_float("DEV_SNIPE_POLL_INTERVAL_SECS", 15.0))
    dev_snipe_watchlist_file: str = field(
        default_factory=lambda: os.getenv("DEV_SNIPE_WATCHLIST_FILE", "logs/dev_snipe_watchlist.json"))
    dev_snipe_watchlist_lock_file: str = field(
        default_factory=lambda: os.getenv("DEV_SNIPE_WATCHLIST_LOCK_FILE", "logs/dev_snipe_watchlist.lock"))
    dev_snipe_state_file: str = field(
        default_factory=lambda: os.getenv("DEV_SNIPE_STATE_FILE", "logs/dev_snipe_state.json"))
    dev_snipe_state_lock_file: str = field(
        default_factory=lambda: os.getenv("DEV_SNIPE_STATE_LOCK_FILE", "logs/dev_snipe_state.lock"))

    # -- SnipeX signal source (bot/snipex.py) ------------------------------
    # Built 2026-09-15, fourth piece of the CopyTrade toolkit -- built AHEAD
    # of Token Snipe a second time, at the user's explicit request ("yes
    # SnipeX next"). Same "signal source only" architecture as every other
    # piece -- see bot/wallet_follow.py's own module docstring.
    #
    # Watches specific X/Twitter accounts for new posts (including replies)
    # containing a token contract address, buying via the same shared path
    # as everything else. Credential is xAI's `x_search` tool
    # (docs.x.ai/developers/tools/x-search), NOT a real X/Twitter Developer
    # credential -- the user only had xAI (Grok) API keys on hand, and
    # chose to proceed with those rather than wait on X's own developer
    # approval process (explicit, informed tradeoff, not a default -- see
    # this piece's own real-mechanism writeup in
    # [[project_gmgn_copytrade_toolkit_research_20260914]] for the full
    # reasoning, including a genuine injection-risk discussion).
    #
    # MECHANISM, verified live 2026-09-15 before writing any code:
    #   - `x_search`'s `allowed_x_handles` parameter only FILTERS results --
    #     it does NOT tell Grok which account to search. The handle must be
    #     named explicitly in the natural-language prompt every single poll,
    #     or Grok has nothing to search for (confirmed live: naming only in
    #     allowed_x_handles produced "I don't have a specific account...";
    #     naming it in the prompt text worked correctly).
    #   - `from_date`/`to_date` genuinely constrain results (confirmed live
    #     against a real historical window) -- used for a rolling
    #     "since last poll" search window, real API support, not guessed.
    #   - X status IDs (parsed from the returned post URLs) are reliably
    #     time-ordered (Twitter Snowflake IDs, confirmed against two real
    #     time-separated test windows) -- used as the actual "what's new"
    #     dedup anchor, same role create_timestamp/timestamp play for
    #     dev-snipe/wallet-follow, since this tool has no clean structured
    #     timestamp field of its own to read.
    #   - Every candidate contract address is extracted from the exact
    #     quoted post text with a strict regex, never trusted purely to
    #     Grok's own paraphrase, and still has to clear the same
    #     security.check_arbitrary_token_candidate() real on-chain check
    #     every other signal source uses before anything can buy -- bounds
    #     (does not eliminate) the real injection risk of watching
    #     human-authored social text.
    #   - Scope, stated honestly: covers new posts/replies by the watched
    #     account. Does NOT yet detect reposts of someone else's content or
    #     profile-bio changes specifically -- those were part of the
    #     original SnipeX tool's spec but aren't verified working through
    #     this mechanism, not claimed as covered.
    #
    # Pilot handle: Cented (@Cented7) -- the SAME wallet already chosen for
    # wallet-follow, cross-confirmed via GMGN's own portfolio stats
    # (common.twitter_bind=True, common.is_blue_verified=True,
    # common.followers_count=810,843), not a cold guess. User explicitly
    # delegated the handle choice ("you decide but get it done please") --
    # this was the only real, already-vetted candidate found (checked
    # Cented plus the dev-snipe pilot wallet plus 5 other real creator
    # wallets touched earlier the same day; only Cented had a linked X
    # account at all).
    snipex_enabled: bool = field(default_factory=lambda: _get_bool("SNIPEX_ENABLED", False))
    # Each poll is a full LLM completion (real $ cost: xAI's own $5/1k
    # posts fetched, plus normal Grok token cost), not a cheap structured
    # API call like every other signal source's poll -- deliberately much
    # slower default than wallet-follow's 12s/dev-snipe's 15s.
    snipex_poll_interval_secs: float = field(
        default_factory=lambda: _get_float("SNIPEX_POLL_INTERVAL_SECS", 45.0))
    snipex_model: str = field(default_factory=lambda: os.getenv("SNIPEX_MODEL", "grok-4.6"))
    snipex_watchlist_file: str = field(
        default_factory=lambda: os.getenv("SNIPEX_WATCHLIST_FILE", "logs/snipex_watchlist.json"))
    snipex_watchlist_lock_file: str = field(
        default_factory=lambda: os.getenv("SNIPEX_WATCHLIST_LOCK_FILE", "logs/snipex_watchlist.lock"))
    snipex_state_file: str = field(
        default_factory=lambda: os.getenv("SNIPEX_STATE_FILE", "logs/snipex_state.json"))
    snipex_state_lock_file: str = field(
        default_factory=lambda: os.getenv("SNIPEX_STATE_LOCK_FILE", "logs/snipex_state.lock"))

    # -- SkyEyeCopy signal source (bot/skyeye_copy.py) --------------------
    # Built 2026-09-15, fifth and final piece of the CopyTrade toolkit --
    # added to the queue after the user asked whether it had been built
    # already and it had not, built last after the other four. Same
    # "signal source only" architecture as every other piece -- see
    # bot/wallet_follow.py's own module docstring for the full reasoning.
    # No sell-side special-casing per wallet like wallet-follow -- see
    # bot/skyeye_copy.py's own module docstring for why a full close from
    # ANY smart-money wallet counts as a real exit signal here, not just
    # the specific wallet that triggered the original buy.
    #
    # Unlike WalletCopy/wallet-follow (ONE wallet you pick), SkyEyeCopy
    # auto-follows GMGN's OWN curated "smart money" wallet list via
    # `track smartmoney` -- a global, platform-tagged feed (exist auth
    # only, weight 1, no Track-first requirement -- confirmed real via
    # gmgn-cli's own skills/gmgn-track/SKILL.md). Deliberately does NOT use
    # GMGN's own "AI Filter" (an opaque, unverifiable proprietary toggle
    # flagged for real skepticism during the original toolkit scoping) --
    # runs the same transparent security.check_arbitrary_token_candidate()
    # every other signal source uses instead.
    #
    # CRITICAL, confirmed via real documentation before building anything:
    # `is_open_or_close` means the OPPOSITE thing here vs. `follow-wallet`
    # -- for kol/smartmoney specifically, 0 = position opened/added,
    # 1 = position closed/reduced (follow-wallet is the reverse: 1=full
    # open/close, 0=partial). Getting this backwards would flip buy/sell
    # logic entirely -- do not reuse follow-wallet's own constant/logic
    # for this endpoint without re-checking.
    #
    # Deliberately kept simple at the user's explicit request ("keep
    # simple and we can always change if I decide ok") -- fires on ANY
    # single qualifying smart-money buy, does NOT require GMGN's own
    # documented "cluster signal" (3+ wallets converging within ~30 min)
    # before acting, even though that's flagged in the real docs as a
    # stronger conviction signal. Can be tightened later if real data
    # shows single-wallet signals are too noisy.
    skyeye_copy_enabled: bool = field(default_factory=lambda: _get_bool("SKYEYE_COPY_ENABLED", False))
    # Cheapest endpoint of any signal source (weight 1 vs wallet-follow's
    # 3/dev-snipe's 2+3) -- but this is a much higher-volume aggregate feed
    # (every GMGN-tagged smart money wallet, not one target), so the poll
    # interval is set for reasonable signal pacing, not API cost.
    skyeye_copy_poll_interval_secs: float = field(
        default_factory=lambda: _get_float("SKYEYE_COPY_POLL_INTERVAL_SECS", 15.0))
    # Page size per poll -- 100 matches the CLI's own documented default.
    skyeye_copy_fetch_limit: int = field(
        default_factory=lambda: _get_int("SKYEYE_COPY_FETCH_LIMIT", 100))
    # Real trades can share the exact same timestamp (confirmed live,
    # 2026-09-15 -- one wallet logged multiple buys of the same token
    # within the same second), which rules out the simple "last_seen_ts"
    # scalar cursor every other signal source uses -- a timestamp alone
    # can't reliably distinguish "already processed" from "new" when
    # several trades land on the identical second. Dedup is instead a
    # rolling set of recently-seen transaction hashes, capped at this many
    # entries (oldest trimmed first) -- comfortably covers several poll
    # cycles worth of the max fetch_limit above even with some overlap,
    # without letting the state file grow unbounded.
    skyeye_copy_seen_hashes_limit: int = field(
        default_factory=lambda: _get_int("SKYEYE_COPY_SEEN_HASHES_LIMIT", 2000))
    skyeye_copy_state_file: str = field(
        default_factory=lambda: os.getenv("SKYEYE_COPY_STATE_FILE", "logs/skyeye_copy_state.json"))
    skyeye_copy_state_lock_file: str = field(
        default_factory=lambda: os.getenv("SKYEYE_COPY_STATE_LOCK_FILE", "logs/skyeye_copy_state.lock"))

    # -- AI Free Will review loop (bot/ai_review.py) ---------------------
    # Mirrors Axiom-bot's own bot/performance_evaluator.py -- periodic
    # OpenAI review of recent closed trades, may adjust allowlisted
    # strategy settings without bounds or per-review step limits while in
    # paper mode, and writes lessons to a memory file. Built 2026-09-06,
    # user's own explicit request ("he needs to be fully aware and
    # self-learning with free will"). Adjustments are
    # applied via ai_overrides_file, re-read live by bot/exit_manager.py
    # every check cycle (NOT this static config object) so a change takes
    # effect immediately, no restart -- same reasoning Axiom-bot's own
    # settings_override.json pattern uses.
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    # xAI (Grok) API key, used by bot/snipex.py's x_search tool -- see that
    # module's own docstring and this project's SnipeX config section above
    # for the real mechanism this powers.
    xai_api_key: str = field(default_factory=lambda: os.getenv("XAI_API_KEY", ""))
    ai_review_every_n_trades: int = field(default_factory=lambda: _get_int("AI_REVIEW_EVERY_N_TRADES", 5))
    # Notable-outcome threshold (2026-10-16): a closed trade whose |pnl_pct|
    # exceeds this is flagged as a "notable outcome" and advances the AI Free
    # Will review early -- so the Gentleman reacts to a single big win or
    # big loss immediately instead of waiting for N accumulated trades.
    # Absolute pct (e.g. 15 = +/-15%); 0 disables this early-trigger.
    ai_review_notable_pnl_pct: float = field(
        default_factory=lambda: _get_float("AI_REVIEW_NOTABLE_PNL_PCT", 15.0))
    ai_memory_file: str = field(default_factory=lambda: os.getenv("AI_MEMORY_FILE", "logs/ai_memory.json"))
    gentleman_chat_log_file: str = field(
        default_factory=lambda: os.getenv("GENTLEMAN_CHAT_LOG_FILE", "logs/gentleman_chat.jsonl"))
    gentleman_chat_log_lock_file: str = field(
        default_factory=lambda: os.getenv("GENTLEMAN_CHAT_LOG_LOCK_FILE", "logs/gentleman_chat.lock"))
    ai_overrides_file: str = field(
        default_factory=lambda: os.getenv("AI_OVERRIDES_FILE", "logs/ai_overrides.json"))
    # Cross-process locks (2026-09-09, same real bug class as
    # paper_portfolio_lock_file/live_positions_lock_file above -- Claude's
    # own write_user_note() calls run as separate ad-hoc `python -c`
    # processes, invoked repeatedly through the night while bot.screener's
    # own review loop (maybe_review_performance(), a real multi-second
    # OpenAI call in the middle of its own read-modify-write cycle) could
    # genuinely be in flight at the same moment -- a note landing in that
    # window would get silently clobbered when the stale in-progress review
    # saves back over it. See bot/file_lock.py's own docstring.
    ai_memory_lock_file: str = field(
        default_factory=lambda: os.getenv("AI_MEMORY_LOCK_FILE", "logs/ai_memory.lock"))
    ai_overrides_lock_file: str = field(
        default_factory=lambda: os.getenv("AI_OVERRIDES_LOCK_FILE", "logs/ai_overrides.lock"))

    # AI judgment shadow mode (2026-09-18, user's explicit request: "let
    # him watch the market real close" and decide sell/hold on his own,
    # but "let's go with number three -- this is testing, no capital
    # involved" -- WATCH AND LOG ONLY, no real sell authority. See
    # bot/ai_judgment.py's own module docstring for the full design.
    ai_judgment_enabled: bool = field(default_factory=lambda: _get_bool("AI_JUDGMENT_ENABLED", True))
    ai_judgment_interval_secs: float = field(
        default_factory=lambda: _get_float("AI_JUDGMENT_INTERVAL_SECS", 5.0))
    ai_judgment_log_file: str = field(
        default_factory=lambda: os.getenv("AI_JUDGMENT_LOG_FILE", "logs/ai_judgment_log.json"))

    # -- Voice interaction (bot/voice_io.py, tui.py `v` hotkey) ------------
    # Built 2026-10-15 at the user's explicit request for hands-free
    # command/alert while watching the dashboard. Deliberately OFF by
    # default -- the mic never opens on its own, first run is a deliberate
    # .env flip, same "harder to trigger by accident" reasoning that
    # retired the `l` hotkey after the 2026-09-16 live-mode incident.
    voice_enabled: bool = field(default_factory=lambda: _get_bool("VOICE_ENABLED", False))
    # tiny/base/small -- local faster-whisper only, no API keys by design.
    voice_stt_model: str = field(default_factory=lambda: os.getenv("VOICE_STT_MODEL", "tiny"))
    voice_stt_compute: str = field(default_factory=lambda: os.getenv("VOICE_STT_COMPUTE", "int8"))
    # Spoken alerts (position closed / buy fired) independent of the mic.
    voice_speak_alerts: bool = field(default_factory=lambda: _get_bool("VOICE_SPEAK_ALERTS", True))
    # Spoken echo of every command result too (arming, refusals, closes).
    voice_speak_feedback: bool = field(default_factory=lambda: _get_bool("VOICE_SPEAK_FEEDBACK", True))
    # Substring of the Windows SAPI voice name ("David", "Zira", ...) --
    # empty keeps the system default.
    voice_tts_voice: str = field(default_factory=lambda: os.getenv("VOICE_TTS_VOICE", ""))
    # sounddevice input device index (see the voice_io input[N] lines it
    # logs at startup). -1 = the Windows default. PREFER voice_input_name
    # over this: indices RENUMBER whenever a device is enabled/disabled
    # (proved live 2026-10-15: enabling Microphone Array shifted every
    # PortAudio index on this machine), so a hardcoded number silently
    # binds to a different device after any Windows sound change.
    voice_input_device: int = field(default_factory=lambda: _get_int("VOICE_INPUT_DEVICE", -1))
    # Substring match against device names from the startup diagnostic
    # (e.g. "Microphone Array", "Realtek HD Audio Mic"). Resolved EVERY
    # boot, so it survives renumbering. Wins over voice_input_device.
    voice_input_name: str = field(
        default_factory=lambda: os.getenv("VOICE_INPUT_NAME", "Microphone Array"))
    voice_tts_rate: int = field(default_factory=lambda: _get_int("VOICE_TTS_RATE", 185))
    # Money-touching voice actions (sell all / close N / shutdown) must be
    # RE-SPOKEN as "confirm" within this window before executing -- any
    # other utterance cancels. The keyboard does these zero-confirm;
    # voice earns the extra gate because a mis-heard word is a worse
    # accidental trigger than a mis-pressed key (2026-09-16 "l" incident).
    voice_confirm_window_secs: float = field(
        default_factory=lambda: _get_float("VOICE_CONFIRM_WINDOW_SECS", 8.0))


config = Config()
