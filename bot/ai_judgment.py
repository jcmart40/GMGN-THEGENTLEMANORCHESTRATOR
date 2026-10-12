"""AI Free Will's own real-time judgment on OPEN positions -- WATCH AND
LOG ONLY, no sell authority (2026-09-18, user's explicit request: "let
him watch the market real close" and learn to see a trade turning, but
"let's go with number three -- this is testing, no capital involved").

Every real exit decision still goes through bot/exit_manager.py's
deterministic rules (stop-loss, take-profit, emergency-drawdown, max-hold,
etc.), completely unaffected by anything in this module. This exists
purely to build up a real record of whether the AI's own judgment on a
still-open position would have agreed or disagreed with what actually
happened -- before ever considering giving it real authority to act on
that judgment.

Runs as its OWN independent loop (see run_forever()), never blocking
bot/exit_manager.py's fast, mechanical exit_check_interval_secs cycle --
the exact mistake Axiom-bot's own history has a real, documented incident
for (a blocking OpenAI call sitting directly in the sell path, found and
fixed 2026-08-25). The OpenAI call here runs via asyncio.to_thread, same
safe-offload pattern bot/ai_review.py's own review loop already uses.

Reads position data that's ALREADY fresh -- last_price/peak_pnl_pct are
updated every cycle by bot/exit_manager.py's own price-check loop -- so
this module makes ZERO extra market-data API calls, only the OpenAI call
itself.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time

import bot.executor as executor
import bot.paper_trader as paper_trader
import bot.trading_control as trading_control
from config import config

log = logging.getLogger("gmgn-agent")


def _pnl_pct(entry_price: float, current_price: float) -> float:
    if not entry_price:
        return 0.0
    return (current_price - entry_price) / entry_price * 100.0


def _append_judgment(entry: dict) -> None:
    """Same bounded-append-atomic-write pattern as every other staging
    file in this project."""
    try:
        directory = os.path.dirname(config.ai_judgment_log_file) or "."
        os.makedirs(directory, exist_ok=True)
        existing = []
        if os.path.exists(config.ai_judgment_log_file):
            try:
                with open(config.ai_judgment_log_file, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []
        existing.append(entry)
        existing = existing[-1000:]
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        os.replace(tmp, config.ai_judgment_log_file)
    except Exception:
        log.exception("Failed to record AI judgment entry for %s -- watch-only, "
                       "no effect on the actual position either way.", entry.get("symbol"))


def _build_prompt(pos: dict, pnl_pct: float, age_secs: float) -> str:
    return f"""You are watching one of your own OPEN trading positions in real time. You do
NOT have authority to act -- this is a judgment exercise only, your answer is
logged for later review, nothing executes on it.

Position:
- Symbol: {pos.get("symbol")}
- Entry price: {pos.get("entry_price")}
- Current price: {pos.get("last_price", pos.get("entry_price"))}
- Current PnL: {pnl_pct:.2f}%
- Peak PnL since entry: {pos.get("peak_pnl_pct", 0.0):.2f}%
- Age: {age_secs:.0f} seconds
- Entry market cap: ${pos.get("entry_market_cap", 0):.0f}
- Source: {pos.get("source")}

Based on this trajectory (rising, falling, or flat), what would YOU do right
now -- sell or hold? Be specific about what in these numbers drove your call,
not a generic answer.

Respond in JSON: {{"decision": "sell" or "hold", "reasoning": "<one or two sentences, specific to these numbers>"}}"""


async def _judge_one(address: str, pos: dict, mode: str) -> None:
    entry_price = pos.get("entry_price", 0.0)
    current_price = pos.get("last_price", entry_price)
    pnl_pct = _pnl_pct(entry_price, current_price)
    age_secs = time.time() - pos.get("entry_ts", time.time())

    prompt = _build_prompt(pos, pnl_pct, age_secs)
    try:
        def _call():
            from openai import OpenAI
            client = OpenAI(api_key=config.openai_api_key)
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=150,
            )
            return json.loads(response.choices[0].message.content)
        result = await asyncio.to_thread(_call)
    except Exception:
        log.exception("AI judgment call failed for %s -- skipping this cycle, watch-only anyway.",
                       pos.get("symbol"))
        return

    entry = {
        "ts": time.time(),
        "address": address,
        "symbol": pos.get("symbol"),
        "mode": mode,
        "pnl_pct": pnl_pct,
        "peak_pnl_pct": pos.get("peak_pnl_pct", 0.0),
        "age_secs": age_secs,
        "decision": result.get("decision"),
        "reasoning": result.get("reasoning"),
    }
    _append_judgment(entry)
    log.info("[bold cyan]AI JUDGMENT (watch-only)[/bold cyan] %s pnl=%.2f%% age=%.0fs -> %s: %s",
              pos.get("symbol"), pnl_pct, age_secs, (result.get("decision") or "?").upper(),
              result.get("reasoning"))


async def judge_open_positions_once() -> None:
    if not config.ai_judgment_enabled or not config.openai_api_key:
        return
    mode = trading_control.get_mode()
    if mode == "paper":
        positions = paper_trader.get_portfolio()["positions"]
    elif mode == "live":
        positions = executor.get_positions()["positions"]
    else:
        return
    if not positions:
        return
    # Concurrent, not sequential (same reasoning as every other
    # parallelized-call fix this project has made) -- paying the slowest
    # single OpenAI call once, not the sum across every open position.
    await asyncio.gather(
        *(_judge_one(addr, pos, mode) for addr, pos in positions.items()),
        return_exceptions=True,
    )


async def run_forever() -> None:
    while True:
        try:
            await judge_open_positions_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad judgment pass must never kill this loop
            log.exception("AI judgment pass crashed -- continuing on the next cycle.")
        await asyncio.sleep(config.ai_judgment_interval_secs)
