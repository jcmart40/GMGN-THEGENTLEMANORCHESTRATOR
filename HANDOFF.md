# GMGN-bot — handoff (2026-09-06, fully autonomous rebuild)

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
