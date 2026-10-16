# GMGN-bot — handoff (2026-09-06, fully autonomous rebuild)

## 2026-10-15 — TOMORROW: The Gentleman is STUCK (rate-limit spin loop) + flicker

**State: the bot process is effectively hung and needs a clean restart before
any new work. Paper mode, dash is clean, no real money at risk.**

1. HE IS STUCK. Do NOT debug as-is — restart his screener engine.
   - Last activity 19:08:33 (10/15); 45+ min of zero writes since.
   - Process alive but CPU-spinning (~943s CPU in 51 min): stuck in a tight
     GMGN rate-limit retry loop that is NOT respecting the backoff sleep.
   - Trigger: GMGN rate-limited hard ~19:06, candidates rejected
     "rate-limited (fail-closed)". Normal backoff (RATE_LIMIT_MAX_CONSECUTIVE
     =3 / COOLDOWN=45 / BASE=5s) should sleep it out; it spun instead.
   - FIX: kill the stuck screener PIDs and launch fresh `python -m bot.screener`.
   - Follow-up worth investigating: why the backoff loop spun instead of
     sleeping — check screener.py's rate-limit catch path (did a bare `except`
     or a non-sleep retry swallow the 429 backoff?).

2. FLICKER CAME BACK. The running dashboard (started 10/15 19:02) predates
   commit 98552a8, the screen=False flicker fix. Restart the dashboard with a
   SINGLE fresh `python tui.py`; do not keep two tui.py processes (a parent
   forks a child — kill the whole tree and start ONE).

**Commands for tomorrow (after killing the stuck processes):**
   - `python -m bot.screener`  (engine; runs screener + exit_manager + AI review)
   - `python tui.py`           (ONE dashboard window only)

**Also see earlier ready-state notes, and note `GMGN_ALLOW_AUTOMATED_TRADES=1`
+ TRADING_MODE=paper + OPENAI_API_KEY set are all active from this session.**

## Operator dashboard-reset instruction (2026-10-13)

When the operator says "clear the dash" or asks to clear all trading data for
a fresh run, stop the bot first and verify it is stopped. Archive the current
paper/live portfolio and PnL state, dashboard trade history, screening/buy
activity, AI judgment log, and trade-burst state before resetting them to
empty defaults. Do not erase the master trade-history log or AI learning
memory/lessons; preserve seen-mint and signal-source cursors to avoid
reprocessing tokens and duplicate buys. Keep the configured trading mode
unchanged.

**Architecture changed completely from the 9/5 version of this file.** GMGN-bot
is now fully autonomous, matching Axiom-bot's own model — no human approval
gate. The instant a token clears `bot/security.py`'s screening, the bot buys
it. There is no pending-candidate queue anymore.

## Done and verified tonight
- `bot/screener.py` — buys autonomously on clear (`_autonomous_buy()`).
  `run_forever()` now gathers the screening loop AND `bot/exit_manager.py`'s
  loop together in one process — this process (`python -m bot.screener`) is
  GMGN-bot's closest analog to Axiom's `main.py`. Still no separate main.py
  file, by design.
- `bot/exit_manager.py` (NEW) — autonomous stop-loss (20%) / take-profit (50%)
  for every open position, paper and live. Verified live: caught a real -38%
  crash and auto-closed it correctly. These thresholds are NOT backtested,
  just a starting point (`STOP_LOSS_PCT`/`TAKE_PROFIT_PCT`/
  `EXIT_CHECK_INTERVAL_SECS` in `.env`).
- `bot/executor.py` — fixed a real gap: `execute_buy()` now records
  `entry_price` (previously never stored, so live positions couldn't have
  PnL computed at all).
- `tui.py` — rebuilt to drop the obsolete Approval Queue panel/hotkeys,
  replaced with "Recent Autonomous Buys." Kept: `x` (close position early),
  `o`/`r`/`l` (mode off/paper/live), `q` (quit). No confirmation prompts,
  matching Axiom's own zero-confirmation convention on high-stakes keys.
- Project moved to `C:\Users\jcmar\OneDrive\Desktop\Bot Folders\GMGN-bot`;
  the `GMGN Agent` shortcut (pinned to taskbar) points here now.

## Still pending — the next two things to build
1. **Trade-outcome "watcher"** — a single, unified trade-history log both
   `paper_trader.simulate_sell()` and `executor.execute_sell()` write to on
   every close (mint, entry/exit price, pnl, reason, paper-or-live, timestamps),
   independent of whatever triggered the close (stop-loss, take-profit, or a
   manual `tui.py` close). Right now paper and live closes are recorded in two
   separate files with no consolidated view — build this first, since the AI
   Free Will loop below needs real data to review.
2. **AI Free Will / self-learning loop** — periodic OpenAI-based review of
   recent trades (once the watcher above exists) that adjusts the bot's own
   `stop_loss_pct`/`take_profit_pct` and writes lessons to memory. Mirrors
   Axiom-bot's `bot/performance_evaluator.py`. GMGN-bot has no `OPENAI_API_KEY`
   configured yet — Axiom-bot's own key exists in its `.env` if reusing it.

## Where things are
- Trading mode: left on **paper** at handoff.
- Real GMGN wallet address and balance are intentionally omitted from the shared repository.
- `GMGN_ALLOW_AUTOMATED_TRADES` is NOT set — live execution can't actually
  fire yet even if mode is switched to "live."
